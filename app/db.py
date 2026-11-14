from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    owner_id        BIGINT PRIMARY KEY,
    owner_chat_id   BIGINT,
    connection_id   TEXT UNIQUE,
    is_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
    can_reply       BOOLEAN NOT NULL DEFAULT FALSE,
    can_read        BOOLEAN NOT NULL DEFAULT FALSE,
    business_name   TEXT NOT NULL DEFAULT '',
    about           TEXT NOT NULL DEFAULT '',
    tone            TEXT NOT NULL DEFAULT '',
    cta             TEXT NOT NULL DEFAULT '',
    sales_mode      BOOLEAN NOT NULL DEFAULT TRUE,
    paused          BOOLEAN NOT NULL DEFAULT FALSE,
    test_mode       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES tenants(owner_id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id          SERIAL PRIMARY KEY,
    source_id   INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    owner_id    BIGINT NOT NULL,
    content     TEXT NOT NULL,
    tsv         TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_owner_idx ON chunks (owner_id);

CREATE TABLE IF NOT EXISTS links (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES tenants(owner_id) ON DELETE CASCADE,
    topic       TEXT NOT NULL,
    url         TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chats (
    owner_id    BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    human_until TIMESTAMPTZ,
    PRIMARY KEY (owner_id, chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL,          -- user | assistant
    author      TEXT NOT NULL,          -- customer | bot | owner
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_chat_idx ON messages (owner_id, chat_id, id DESC);

CREATE TABLE IF NOT EXISTS escalations (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    chat_id         BIGINT NOT NULL,
    connection_id   TEXT NOT NULL,
    last_message_id BIGINT NOT NULL,
    question        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    notify_chat_id  BIGINT,
    notify_msg_id   BIGINT,
    answer          TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS notify_chat_id BIGINT;
ALTER TABLE escalations ADD COLUMN IF NOT EXISTS answer TEXT;
CREATE INDEX IF NOT EXISTS escalations_notify_idx ON escalations (notify_chat_id, notify_msg_id);

CREATE TABLE IF NOT EXISTS alert_messages (
    escalation_id INT NOT NULL,
    chat_id       BIGINT NOT NULL,
    msg_id        BIGINT NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS past_messages (
    username TEXT NOT NULL,
    idx      INT NOT NULL,
    ts       TEXT NOT NULL,
    who      TEXT NOT NULL,
    content  TEXT NOT NULL,
    PRIMARY KEY (username, idx)
);
"""

pool: asyncpg.Pool | None = None


async def init() -> None:
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)


def _p() -> asyncpg.Pool:
    assert pool is not None, "db.init() was not called"
    return pool


# ---------- tenants ----------

async def ensure_tenant(owner_id: int, owner_chat_id: int | None = None) -> asyncpg.Record:
    return await _p().fetchrow(
        """INSERT INTO tenants (owner_id, owner_chat_id) VALUES ($1, $2)
           ON CONFLICT (owner_id) DO UPDATE
             SET owner_chat_id = COALESCE(EXCLUDED.owner_chat_id, tenants.owner_chat_id)
           RETURNING *""",
        owner_id, owner_chat_id,
    )


async def get_tenant(owner_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM tenants WHERE owner_id = $1", owner_id)


async def tenant_by_connection(connection_id: str) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM tenants WHERE connection_id = $1", connection_id)


async def save_connection(owner_id: int, owner_chat_id: int, connection_id: str,
                          enabled: bool, can_reply: bool, can_read: bool) -> asyncpg.Record:
    await ensure_tenant(owner_id, owner_chat_id)
    # One account = one connection. Drop the id from any older row first (unique constraint).
    await _p().execute("UPDATE tenants SET connection_id = NULL WHERE connection_id = $1 AND owner_id <> $2",
                       connection_id, owner_id)
    return await _p().fetchrow(
        """UPDATE tenants SET connection_id=$2, is_enabled=$3, can_reply=$4, can_read=$5,
                              owner_chat_id=$6
           WHERE owner_id=$1 RETURNING *""",
        owner_id, connection_id, enabled, can_reply, can_read, owner_chat_id,
    )


TENANT_FIELDS = {"business_name", "about", "tone", "cta", "sales_mode", "paused", "test_mode"}


async def set_tenant_field(owner_id: int, field: str, value) -> None:
    if field not in TENANT_FIELDS:
        raise ValueError(field)
    await _p().execute(f"UPDATE tenants SET {field} = $2 WHERE owner_id = $1", owner_id, value)


# ---------- knowledge ----------

async def add_source(owner_id: int, title: str, kind: str, content: str, chunks: list[str]) -> int:
    async with _p().acquire() as con, con.transaction():
        sid = await con.fetchval(
            "INSERT INTO sources (owner_id, title, kind, content) VALUES ($1,$2,$3,$4) RETURNING id",
            owner_id, title, kind, content,
        )
        await con.executemany(
            "INSERT INTO chunks (source_id, owner_id, content) VALUES ($1,$2,$3)",
            [(sid, owner_id, c) for c in chunks],
        )
    return sid


async def list_sources(owner_id: int) -> list[asyncpg.Record]:
    return await _p().fetch(
        "SELECT id, title, kind, length(content) AS size, created_at FROM sources "
        "WHERE owner_id=$1 ORDER BY id", owner_id)


async def delete_source(owner_id: int, source_id: int) -> bool:
    res = await _p().execute("DELETE FROM sources WHERE owner_id=$1 AND id=$2", owner_id, source_id)
    return res.endswith("1")


async def knowledge_size(owner_id: int) -> int:
    return await _p().fetchval(
        "SELECT COALESCE(SUM(length(content)),0) FROM sources WHERE owner_id=$1", owner_id)


async def all_sources(owner_id: int) -> list[asyncpg.Record]:
    return await _p().fetch("SELECT id, title, content FROM sources WHERE owner_id=$1 ORDER BY id", owner_id)


async def search_chunks(owner_id: int, tsquery: str, limit: int) -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT c.content, s.title, ts_rank(c.tsv, q) AS rank
           FROM chunks c JOIN sources s ON s.id = c.source_id, to_tsquery('simple', $2) q
           WHERE c.owner_id = $1 AND c.tsv @@ q
           ORDER BY rank DESC LIMIT $3""",
        owner_id, tsquery, limit,
    )


# ---------- referral links ----------

async def add_link(owner_id: int, topic: str, url: str, note: str) -> int:
    return await _p().fetchval(
        "INSERT INTO links (owner_id, topic, url, note) VALUES ($1,$2,$3,$4) RETURNING id",
        owner_id, topic, url, note)


async def list_links(owner_id: int) -> list[asyncpg.Record]:
    return await _p().fetch("SELECT * FROM links WHERE owner_id=$1 ORDER BY id", owner_id)


async def delete_link(owner_id: int, link_id: int) -> bool:
    res = await _p().execute("DELETE FROM links WHERE owner_id=$1 AND id=$2", owner_id, link_id)
    return res.endswith("1")


# ---------- chats & history ----------

async def human_active(owner_id: int, chat_id: int) -> bool:
    until = await _p().fetchval(
        "SELECT human_until FROM chats WHERE owner_id=$1 AND chat_id=$2", owner_id, chat_id)
    return until is not None and until > datetime.now(timezone.utc)


async def set_human(owner_id: int, chat_id: int, minutes: int | None) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes else None
    await _p().execute(
        """INSERT INTO chats (owner_id, chat_id, human_until) VALUES ($1,$2,$3)
           ON CONFLICT (owner_id, chat_id) DO UPDATE SET human_until = EXCLUDED.human_until""",
        owner_id, chat_id, until)


async def add_message(owner_id: int, chat_id: int, role: str, author: str, content: str) -> None:
    await _p().execute(
        "INSERT INTO messages (owner_id, chat_id, role, author, content) VALUES ($1,$2,$3,$4,$5)",
        owner_id, chat_id, role, author, content)


async def history(owner_id: int, chat_id: int, limit: int) -> list[asyncpg.Record]:
    rows = await _p().fetch(
        "SELECT role, author, content FROM messages WHERE owner_id=$1 AND chat_id=$2 "
        "ORDER BY id DESC LIMIT $3", owner_id, chat_id, limit)
    return list(reversed(rows))


async def clear_history(owner_id: int, chat_id: int) -> None:
    await _p().execute("DELETE FROM messages WHERE owner_id=$1 AND chat_id=$2", owner_id, chat_id)


async def bot_replies_today(owner_id: int) -> int:
    return await _p().fetchval(
        "SELECT count(*) FROM messages WHERE owner_id=$1 AND author='bot' "
        "AND created_at > now() - interval '1 day'", owner_id)


# ---------- escalations ----------

async def add_escalation(owner_id: int, chat_id: int, connection_id: str, last_message_id: int,
                         question: str, reason: str) -> int:
    return await _p().fetchval(
        """INSERT INTO escalations (owner_id, chat_id, connection_id, last_message_id, question, reason)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
        owner_id, chat_id, connection_id, last_message_id, question, reason)


async def set_escalation_notify(esc_id: int, notify_chat_id: int, notify_msg_id: int) -> None:
    await _p().execute("UPDATE escalations SET notify_chat_id=$2, notify_msg_id=$3 WHERE id=$1",
                       esc_id, notify_chat_id, notify_msg_id)


async def add_alert_message(esc_id: int, chat_id: int, msg_id: int) -> None:
    """One alert can be sent to several staff chats; each copy can be replied to."""
    await _p().execute("INSERT INTO alert_messages (escalation_id, chat_id, msg_id) VALUES ($1,$2,$3) "
                       "ON CONFLICT DO NOTHING", esc_id, chat_id, msg_id)


async def escalation_by_notify(notify_chat_id: int, notify_msg_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow(
        """SELECT e.* FROM escalations e
           WHERE (e.notify_chat_id=$1 AND e.notify_msg_id=$2)
              OR e.id = (SELECT escalation_id FROM alert_messages WHERE chat_id=$1 AND msg_id=$2)
           LIMIT 1""", notify_chat_id, notify_msg_id)


async def get_escalation(esc_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM escalations WHERE id=$1", esc_id)


async def resolve_escalation(esc_id: int, answer: str) -> None:
    await _p().execute("UPDATE escalations SET resolved=TRUE, answer=$2 WHERE id=$1", esc_id, answer)


async def replace_seed_sources(owner_id: int, items: list[tuple[str, str, list[str]]]) -> None:
    """Swap all file-backed knowledge for the given (title, content, chunks) in one transaction."""
    async with _p().acquire() as con, con.transaction():
        await con.execute("DELETE FROM sources WHERE owner_id=$1 AND kind='seed'", owner_id)
        for title, content, chunks in items:
            sid = await con.fetchval(
                "INSERT INTO sources (owner_id, title, kind, content) VALUES ($1,$2,'seed',$3) RETURNING id",
                owner_id, title, content)
            await con.executemany(
                "INSERT INTO chunks (source_id, owner_id, content) VALUES ($1,$2,$3)",
                [(sid, owner_id, c) for c in chunks])


async def stats(owner_id: int) -> asyncpg.Record:
    return await _p().fetchrow(
        """SELECT
             (SELECT count(*) FROM messages WHERE owner_id=$1 AND author='bot'
                AND created_at > now() - interval '7 days') AS bot_replies,
             (SELECT count(DISTINCT chat_id) FROM messages WHERE owner_id=$1 AND author='customer'
                AND created_at > now() - interval '7 days' AND chat_id <> $1) AS customers,
             (SELECT count(*) FROM escalations WHERE owner_id=$1
                AND created_at > now() - interval '7 days') AS escalations,
             (SELECT count(*) FROM escalations WHERE owner_id=$1 AND NOT resolved) AS open_escalations,
             (SELECT count(*) FROM sources WHERE owner_id=$1) AS sources""",
        owner_id)


# ---------- meta & past chats ----------

async def get_meta(key: str) -> str | None:
    return await _p().fetchval("SELECT value FROM meta WHERE key=$1", key)


async def set_meta(key: str, value: str) -> None:
    await _p().execute("INSERT INTO meta (key, value) VALUES ($1,$2) "
                       "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, value)


async def replace_past_messages(rows: list[tuple]) -> None:
    async with _p().acquire() as con, con.transaction():
        await con.execute("DELETE FROM past_messages")
        await con.copy_records_to_table("past_messages", records=rows,
                                        columns=["username", "idx", "ts", "who", "content"])


async def past_messages(username: str, limit: int) -> list[asyncpg.Record]:
    rows = await _p().fetch("SELECT ts, who, content FROM past_messages WHERE username=$1 "
                            "ORDER BY idx DESC LIMIT $2", username, limit)
    return list(reversed(rows))
