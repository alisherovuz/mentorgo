import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _int(name: str, default: str = "0") -> int:
    raw = _env(name, default).strip()
    return int(raw) if raw else 0


BOT_TOKEN = _env("BOT_TOKEN", "")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
DATABASE_URL = _env("DATABASE_URL", "postgresql://support:support@db:5432/support")

# Claude model. Sonnet is the balanced default; claude-haiku-5-5 is cheaper.
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-5-5")

# Telegram user id of the account that connects the bot (the MentorGO Manager account).
# Only this account can connect and use the admin commands.
OWNER_ID = _int("OWNER_ID")

# Optional: a staff group where "I couldn't answer" alerts go. Anyone in the group can reply
# to an alert and the reply is sent to the customer. Empty = alerts go to the owner's chat with the bot.
NOTIFY_CHAT_ID = _int("NOTIFY_CHAT_ID")

# Folder with the playbook (00_playbook.md) and knowledge files. Re-read on every start.
KNOWLEDGE_DIR = Path(_env("KNOWLEDGE_DIR", str(Path(__file__).resolve().parent.parent / "knowledge")))

# Wait this long after a customer's last message before answering, so 3 quick messages get 1 answer.
DEBOUNCE_SECONDS = float(_env("DEBOUNCE_SECONDS", "4"))

# When a human writes in a chat by hand, the bot stays quiet in that chat for this many minutes.
HUMAN_TAKEOVER_MINUTES = _int("HUMAN_TAKEOVER_MINUTES", "60")

# Knowledge bigger than this is searched instead of sent in full.
FULL_CONTEXT_CHARS = _int("FULL_CONTEXT_CHARS", "250000")
RETRIEVAL_CHUNKS = _int("RETRIEVAL_CHUNKS", "8")

# How many previous messages of the chat the model sees.
HISTORY_LIMIT = _int("HISTORY_LIMIT", "24")

# Daily cap on AI answers (protects the Claude bill). 0 = unlimited.
DAILY_REPLY_LIMIT = _int("DAILY_REPLY_LIMIT", "800")
