"""Chats from before the bot was connected, so it can recognise returning customers.

pastchats/history.enc is encrypted (tools/build_history.py); HISTORY_KEY decrypts it. Loaded into
Postgres once per file version and looked up by the customer's @username.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent.parent / "pastchats" / "history.enc"


async def load() -> int:
    if not config.HISTORY_KEY or not FILE.exists():
        log.warning("Past chats not loaded: %s", "HISTORY_KEY empty" if not config.HISTORY_KEY else f"{FILE} missing")
        return 0
    raw = FILE.read_bytes()
    version = hashlib.sha256(raw).hexdigest()[:16]
    if await db.get_meta("history_version") == version:
        return -1  # already loaded
    from cryptography.fernet import Fernet, InvalidToken
    try:
        data = json.loads(Fernet(config.HISTORY_KEY.encode()).decrypt(raw))
    except InvalidToken:
        log.error("HISTORY_KEY does not match pastchats/history.enc; past chats not loaded")
        return 0
    rows = [(user, i, m["t"], m["who"], m["text"]) for user, msgs in data.items() for i, m in enumerate(msgs)]
    await db.replace_past_messages(rows)
    await db.set_meta("history_version", version)
    return len(data)


async def context_for(username: str | None, limit: int) -> list[dict]:
    """Past messages as history rows (role/author/content), oldest first."""
    if not username:
        return []
    rows = await db.past_messages(username.lower(), limit)
    out = []
    for r in rows:
        tag = f"[eski yozishma, {r['ts'][:10]}] "
        if r["who"] == "manager":
            out.append({"role": "assistant", "author": "owner", "content": tag + r["content"]})
        else:
            out.append({"role": "user", "author": "customer", "content": tag + r["content"]})
    return out
