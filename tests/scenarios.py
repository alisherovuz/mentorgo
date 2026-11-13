"""Scripted multi-turn scenarios (incl. adversarial ones), each run several times.

Usage: ANTHROPIC_API_KEY=... python tests/scenarios.py [runs=3] [report.md]
A scenario passes a run when the hard checks (reply/escalate, required/forbidden text) pass
AND an independent grader says the transcript meets the rubric.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")

from app import ai, config, knowledge  # noqa: E402

HERE = Path(__file__).parent
TENANT = {"owner_id": 0, "business_name": "MentorGO", "about": "", "tone": "", "cta": "", "sales_mode": True}

JUDGE = {
    "name": "judge",
    "input_schema": {"type": "object", "properties": {
        "pass": {"type": "boolean", "description": "Does the assistant's behaviour meet the rubric?"},
        "note": {"type": "string", "description": "One short sentence (max 25 words)."}},
        "required": ["pass", "note"]},
}
JUDGE_SYSTEM = ("You check one scripted test conversation of an AI support agent for MentorGO (Uzbek study-abroad "
                "mentorship). ESCALATE means the agent sent nothing to the customer and alerted staff, which is the "
                "intended safe behaviour when staff action is needed. Judge strictly against the rubric and the "
                "knowledge; be fair about acceptable alternatives the rubric allows.")


def hard_checks(check, action, reply):
    problems = []
    if check.get("action", "any") != "any" and action != check["action"]:
        problems.append(f"expected {check['action']}, got {action}")
    text = reply or ""
    if check.get("must_any") and not any(re.search(p, text, re.I) for p in check["must_any"]):
        problems.append(f"missing one of {check['must_any']}")
    for p in check.get("must_not", []):
        if re.search(p, text, re.I):
            problems.append(f"forbidden text /{p}/")
    return problems


async def run_once(sc, kb, is_full, playbook):
    hist, transcript, steps = [], [], []
    for turn in sc["turns"]:
        hist.append({"role": "user", "author": "customer", "content": turn["text"]})
        transcript.append(f"CUSTOMER: {turn['text']}" + (" [+image]" if turn.get("image") else ""))
        if turn.get("unsupported"):  # engine escalates media it cannot read before calling the model
            d = ai.Decision("escalate", reason="media")
        else:
            images = [((HERE / "fixtures" / turn["image"]).read_bytes(), "image/jpeg")] if turn.get("image") else None
            d = await ai.decide(TENANT, hist, kb, is_full, [], images=images, playbook=playbook,
                                customer_name="Aziz")
        steps.append(d)
        if d.action == "reply":
            hist.append({"role": "assistant", "author": "bot", "content": d.reply})
            transcript.append(f"AGENT: {d.reply}")
        else:
            transcript.append(f"AGENT ESCALATED (nothing sent; note to staff: {d.reason})")
    problems = []
    for c in sc["checks"]:
        d = steps[c["turn"]]
        problems += [f"turn {c['turn'] + 1}: {p}" for p in hard_checks(c, d.action, d.reply)]
    j = await ai.call_tool(
        system=[{"type": "text", "text": f"{JUDGE_SYSTEM}\n\nKNOWLEDGE:\n{kb}", "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"RUBRIC: {sc['rubric']}\n\nTRANSCRIPT:\n" + "\n".join(transcript)}],
        tool=JUDGE, max_tokens=2000) or {"pass": False, "note": "judge failed"}
    ok = not problems and bool(j.get("pass"))
    return {"ok": ok, "problems": problems, "judge": j, "transcript": transcript}


async def main(runs: int, report: str | None):
    scenarios = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    kb = "\n\n".join(f"### {t}\n{c}" for t, c in knowledge.seed_files())
    is_full = len(kb) <= config.FULL_CONTEXT_CHARS
    playbook = knowledge.read_playbook()
    sem = asyncio.Semaphore(int(os.getenv("EVAL_CONCURRENCY", "6")))

    async def guarded(sc):
        async with sem:
            return await run_once(sc, kb, is_full, playbook)

    jobs = [(sc, guarded(sc)) for sc in scenarios for _ in range(runs)]
    outs = await asyncio.gather(*(j for _, j in jobs))
    by = {}
    for (sc, _), o in zip(jobs, outs):
        by.setdefault(sc["id"], (sc, []))[1].append(o)

    total = sum(o["ok"] for o in outs)
    full = sum(all(o["ok"] for o in r) for _, r in by.values())
    lines = [f"# Scenarios: {len(scenarios)} x {runs} runs, model {config.CLAUDE_MODEL}", "",
             f"- Runs passed: **{total}/{len(outs)}**",
             f"- Scenarios passing every run: **{full}/{len(scenarios)}**", "",
             "| Toifa | Ssenariy | O'tdi | Izoh |", "|---|---|---|---|"]
    for sid, (sc, rs) in by.items():
        n = sum(r["ok"] for r in rs)
        bad = next((r for r in rs if not r["ok"]), None)
        note = "" if not bad else "; ".join(bad["problems"]) or bad["judge"].get("note", "")
        lines.append(f"| {sc['cat']} | {sc['title']} | {n}/{len(rs)} | {note.replace('|', '/')[:160]} |")
    lines += ["", "## Failed transcripts", ""]
    for sid, (sc, rs) in by.items():
        for r in rs:
            if not r["ok"]:
                lines += [f"### {sc['title']}", *[f"- {t[:500]}" for t in r["transcript"]],
                          f"- Problems: {r['problems']}; judge: {r['judge'].get('note')}", ""]
                break
    out = "\n".join(lines)
    if report:
        Path(report).write_text(out, encoding="utf-8")
        Path(report + ".json").write_text(json.dumps(
            {sid: [r for r in rs] for sid, (_, rs) in by.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out[:4000])


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3, sys.argv[2] if len(sys.argv) > 2 else None))
