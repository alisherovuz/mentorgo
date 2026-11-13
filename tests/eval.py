"""Replay real customer questions through the agent and grade the answers against what the manager said.

Usage:  ANTHROPIC_API_KEY=... python tests/eval.py tests/eval_cases.jsonl [report.md]
Needs no database or Telegram: it uses the knowledge/ files directly.
"""
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "x")

from app import ai, config, knowledge  # noqa: E402

TENANT = {"owner_id": 0, "business_name": "MentorGO", "about": "", "tone": "", "cta": "", "sales_mode": True}

GRADE_TOOL = {
    "name": "grade",
    "description": "Grade the assistant's handling of one customer message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_ok": {"type": "boolean", "description": "Was reply vs escalate the right call?"},
            "facts": {"type": "string", "enum": ["correct", "minor_issue", "wrong", "not_applicable"],
                      "description": "Are business facts in the reply correct per the manager's real answer and the knowledge? wrong = invented or contradicting price/date/policy."},
            "tone_ok": {"type": "boolean", "description": "Natural, polite, short, like the real manager, in the customer's language."},
            "score": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Overall: 5 = as good as or better than the manager."},
            "note": {"type": "string", "description": "One short sentence (max 30 words) on the main problem, or empty."},
        },
        "required": ["decision_ok", "facts", "tone_ok", "score", "note"],
    },
}

GRADER = """You review an AI support agent for MentorGO (Uzbek study-abroad mentorship). For one real customer message you get: what the human manager actually replied, whether a human action was really required (needs_human), and what the agent did.
The agent can REPLY, ESCALATE (send nothing, staff answer) or HANDOFF (reply AND alert staff to act).
Rules for decision_ok: if needs_human is true, ESCALATE or HANDOFF is right, and a reply that only asks for the info staff need (e.g. a screenshot) is also right; any reply that pretends the action was done (e.g. "to'lovingiz qabul qilindi", "invoys yubordik") is wrong. If needs_human is false, REPLY is right, HANDOFF is acceptable when its reply answers the question, and ESCALATE is acceptable only when the knowledge truly lacks the fact.
Facts in the agent's reply may differ from the manager's when the manager's reply is older than the knowledge; judge against the KNOWLEDGE excerpt first. Penalize any invented price, date, discount or promise hard."""


USAGE = []  # (seconds, input, cache_read, cache_write, output) per agent call
_orig_create = None


def _instrument():
    """Record latency and token usage of every agent call."""
    global _orig_create
    msgs = ai.client().messages
    _orig_create = msgs.create

    async def timed(**kw):
        import time
        t = time.perf_counter()
        r = await _orig_create(**kw)
        is_grader = str(kw["system"][0].get("text", "")).startswith("You review an AI support agent")
        if not is_grader:
            u = r.usage
            USAGE.append((time.perf_counter() - t, u.input_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
                          getattr(u, "cache_creation_input_tokens", 0) or 0, u.output_tokens))
        return r
    msgs.create = timed


async def run_case(case, kb, is_full, playbook, sem):
    async with sem:
        hist = list(case.get("history") or []) + [{"role": "user", "author": "customer", "content": case["customer"]}]
        try:
            d = await ai.decide(TENANT, hist, kb, is_full, [], playbook=playbook)
        except Exception as e:  # keep going
            return case, None, {"score": 0, "note": f"agent error: {e}", "decision_ok": False,
                                "facts": "wrong", "tone_ok": False}
        agent = (f"ESCALATED (reason: {d.reason})" if d.action == "escalate" else
                 f"HANDOFF: replied \"{d.reply}\" and alerted staff ({d.reason})" if d.action == "handoff" else
                 f"REPLIED: {d.reply}")
        shown = "\n".join(f"{'MANAGER' if h['role'] == 'assistant' else 'CUSTOMER'}: {h['content'][:400]}"
                          for h in (case.get("history") or [])[-8:]) or "-"
        prompt = (f"CONVERSATION THE AGENT SAW BEFORE (last turns):\n{shown}\n\n"
                  f"CUSTOMER: {case['customer']}\nEARLIER CONTEXT NOTE: {case.get('context') or '-'}\n"
                  f"MANAGER'S REAL REPLY: {case['manager']}\nNEEDS_HUMAN: {case['needs_human']} "
                  f"({case.get('why_human') or ''})\n\nAGENT: {agent}")
        system = [{"type": "text", "text": f"{GRADER}\n\nKNOWLEDGE (current truth):\n{kb}",
                   "cache_control": {"type": "ephemeral"}}]
        grade = await ai.call_tool(system=system, messages=[{"role": "user", "content": prompt}],
                                   tool=GRADE_TOOL, max_tokens=4096, model=os.getenv("GRADER_MODEL"))
        if grade is None:
            grade = {"score": 0, "note": "grader failed", "decision_ok": False, "facts": "wrong", "tone_ok": False}
        grade = {"decision_ok": bool(grade.get("decision_ok")), "facts": grade.get("facts", "not_applicable"),
                 "tone_ok": bool(grade.get("tone_ok")), "score": int(grade.get("score") or 0),
                 "note": grade.get("note", "")}
        return case, d, grade


async def main(path, report):
    cases = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    kb = "\n\n".join(f"### {t}\n{c}" for t, c in knowledge.seed_files())
    is_full = len(kb) <= config.FULL_CONTEXT_CHARS
    playbook = knowledge.read_playbook()
    sem = asyncio.Semaphore(int(os.getenv("EVAL_CONCURRENCY", "6")))
    _instrument()
    results = await asyncio.gather(*(run_case(c, kb, is_full, playbook, sem) for c in cases))

    n = len(results)
    dec = sum(g["decision_ok"] for _, _, g in results)
    facts = Counter(g["facts"] for _, _, g in results)
    avg = sum(g["score"] for _, _, g in results) / n
    lines = [f"# Eval: {n} real cases, model {config.CLAUDE_MODEL}", "",
             f"- Average score: **{avg:.2f} / 5**",
             f"- Right reply/escalate decision: **{dec}/{n}**",
             f"- Facts: {dict(facts)}",
             f"- Actions: {dict(Counter(d.action for _, d, _ in results if d))}",
             f"- Tone ok: {sum(g['tone_ok'] for _, _, g in results)}/{n}"]
    if USAGE:
        lat = sorted(u[0] for u in USAGE)
        tot = [sum(u[i] for u in USAGE) for i in range(1, 5)]
        lines += [f"- Agent calls: {len(USAGE)}, latency p50 {lat[len(lat)//2]:.1f}s, p95 {lat[int(len(lat)*0.95)-1]:.1f}s",
                  f"- Tokens per call (avg): input {tot[0]/len(USAGE):.0f}, cache read {tot[1]/len(USAGE):.0f}, "
                  f"cache write {tot[2]/len(USAGE):.0f}, output {tot[3]/len(USAGE):.0f}"]
    lines += ["", "## Weakest cases", ""]
    for case, d, g in sorted(results, key=lambda r: r[2]["score"])[:25]:
        agent = "—" if d is None else (f"ESCALATE: {d.reason}" if d.action == "escalate" else
                                      f"HANDOFF ({d.reason}): {d.reply}" if d.action == "handoff" else d.reply)
        lines += [f"### {g['score']}/5 · {case.get('topic', '')} · {case['file']}",
                  f"- Customer: {case['customer'][:400]}", f"- Manager: {case['manager'][:400]}",
                  f"- Agent: {agent[:500]}", f"- Note: {g['note']}", ""]
    out = "\n".join(lines)
    if report:
        open(report, "w", encoding="utf-8").write(out)
        json.dump([{"case": c, "action": d.action if d else None, "reply": d.reply if d else None,
                    "reason": d.reason if d else None, "grade": g} for c, d, g in results],
                  open(report + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(out[:3000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
