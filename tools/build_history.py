"""Build pastchats/history.enc from a folder of exported Telegram chats (one <username>.txt per chat).

Usage: HISTORY_KEY=... python tools/build_history.py <chats_dir> [--exclude name1,name2]
The output is encrypted with Fernet; the bot decrypts it with the same HISTORY_KEY.
Card numbers and phone numbers are masked; only the last 40 messages of each chat are kept.
"""
import json
import os
import re
import sys
from pathlib import Path

from cryptography.fernet import Fernet

HEADER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.+?) -> (.+?):$")
CARD = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
PHONE = re.compile(r"\+?998[\d ()-]{9,14}")
KEEP = 40


def parse(path: Path) -> list[dict]:
    msgs, cur = [], None
    for line in path.read_text(encoding="utf-8", errors="ignore").split("\n"):
        m = HEADER.match(line)
        if m:
            cur = {"t": m[1], "from": m[2], "body": []}
            msgs.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    out = []
    for m in msgs:
        text = "\n".join(m["body"]).strip() or "[rasm yoki fayl]"
        text = PHONE.sub("[telefon]", CARD.sub("[karta]", text))
        out.append({"t": m["t"], "who": "manager" if "MentorGO" in m["from"] else "customer", "text": text[:1500]})
    return out[-KEEP:]


def main() -> None:
    src = Path(sys.argv[1])
    exclude = set()
    if "--exclude" in sys.argv:
        exclude = {x.strip().lower() for x in sys.argv[sys.argv.index("--exclude") + 1].split(",")}
    data = {}
    for f in sorted(src.glob("*.txt")):
        user = f.stem.lower()
        if user in exclude or not re.fullmatch(r"[a-z0-9_]{4,32}", user):
            continue  # internal chats, and files that are not a real @username
        msgs = parse(f)
        if any(m["who"] == "customer" and m["text"] != "[rasm yoki fayl]" for m in msgs):
            data[user] = msgs
    key = os.environ["HISTORY_KEY"].encode()
    out = Path(__file__).resolve().parent.parent / "pastchats" / "history.enc"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(Fernet(key).encrypt(json.dumps(data, ensure_ascii=False).encode()))
    print(f"{len(data)} chats, {sum(len(v) for v in data.values())} messages -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
