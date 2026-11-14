"""Customer conversation flow: buffer -> decide -> reply & mark read, or leave unread & notify owner."""
from __future__ import annotations

import asyncio
import html
import io
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from . import ai, config, db, history, knowledge

log = logging.getLogger(__name__)

UNSUPPORTED = {
    "voice": "[ovozli xabar]",
    "video_note": "[video xabar]",
    "video": "[video]",
    "audio": "[audio]",
    "document": "[fayl]",
}


def describe(message: Message) -> tuple[str, bool]:
    """Text the model will see for this message, and whether it hides content the model can't read."""
    for attr, label in UNSUPPORTED.items():
        if getattr(message, attr, None):
            return (f"{label} {message.caption or ''}".strip(), True)
    if message.photo:
        return (f"[rasm] {message.caption or ''}".strip(), False)
    if message.sticker:
        return (f"[stiker {message.sticker.emoji or ''}]".strip(), False)
    if message.contact:
        return (f"[kontakt: {message.contact.phone_number}]", False)
    if message.location:
        return ("[lokatsiya yubordi]", False)
    return (message.text or message.caption or "[xabar]", False)


@dataclass
class Pending:
    connection_id: str
    customer: User | None
    message_ids: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    unsupported: bool = False
    task: asyncio.Task | None = None


_pending: dict[tuple[int, int], Pending] = {}
_locks: dict[tuple[int, int], asyncio.Lock] = {}


async def enqueue(bot: Bot, owner_id: int, message: Message) -> None:
    key = (owner_id, message.chat.id)
    text, unsupported = describe(message)
    await db.add_message(owner_id, message.chat.id, "user", "customer", text)

    p = _pending.get(key)
    if p is None:
        p = _pending[key] = Pending(message.business_connection_id, message.from_user)
    p.message_ids.append(message.message_id)
    p.texts.append(text)
    p.unsupported |= unsupported
    if message.photo:
        p.photos.append(message.photo[-1].file_id)
    if p.task and not p.task.done():
        p.task.cancel()
    p.task = asyncio.create_task(_after_pause(bot, owner_id, message.chat.id))


async def _after_pause(bot: Bot, owner_id: int, chat_id: int) -> None:
    try:
        await asyncio.sleep(config.DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return
    key = (owner_id, chat_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        p = _pending.pop(key, None)
        if p is None:
            return
        try:
            await _handle(bot, owner_id, chat_id, p)
        except Exception:
            log.exception("Failed to handle chat %s/%s", owner_id, chat_id)
            tenant = await db.get_tenant(owner_id)
            if tenant:
                await escalate(bot, tenant, chat_id, p, "Texnik xatolik (loglarni tekshiring)")


async def _handle(bot: Bot, owner_id: int, chat_id: int, p: Pending) -> None:
    tenant = await db.get_tenant(owner_id)
    if not tenant or not tenant["is_enabled"] or tenant["paused"] or not tenant["can_reply"]:
        return
    if await db.human_active(owner_id, chat_id):
        return  # owner joined the chat while we were waiting
    if p.unsupported:
        await escalate(bot, tenant, chat_id, p, "Mijoz men o'qiy olmaydigan media yubordi")
        return
    if config.DAILY_REPLY_LIMIT and await db.bot_replies_today(owner_id) >= config.DAILY_REPLY_LIMIT:
        await escalate(bot, tenant, chat_id, p, "Kunlik AI javoblar limiti tugadi")
        return

    try:
        await bot.send_chat_action(chat_id, "typing", business_connection_id=p.connection_id)
    except Exception:
        pass

    images = []
    for file_id in p.photos[-3:]:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        images.append((buf.getvalue(), "image/jpeg"))

    decision = await think(tenant, chat_id, images, p.customer.first_name if p.customer else "",
                           p.customer.username if p.customer else None)

    if decision.action in ("reply", "handoff"):
        for part in split(decision.reply):
            await bot.send_message(chat_id, part, business_connection_id=p.connection_id, parse_mode=None)
        await db.add_message(owner_id, chat_id, "assistant", "bot", decision.reply)
        if tenant["can_read"]:
            try:
                await bot.read_business_message(p.connection_id, chat_id, p.message_ids[-1])
            except Exception as e:
                log.warning("read_business_message failed: %s", e)
        if decision.action == "handoff":
            await escalate(bot, tenant, chat_id, p, decision.reason, bot_reply=decision.reply)
    else:
        await escalate(bot, tenant, chat_id, p, decision.reason)


async def think(tenant, chat_id: int, images=None, customer_name: str = "",
                username: str | None = None) -> ai.Decision:
    owner_id = tenant["owner_id"]
    hist = list(await db.history(owner_id, chat_id, config.HISTORY_LIMIT))
    if len(hist) < 10:  # returning customer: show what was said before the bot was connected
        hist = await history.context_for(username, config.PAST_HISTORY_LIMIT) + hist
    recent_customer = " ".join(r["content"] for r in hist[-6:] if r["role"] == "user")
    kb, is_full = await knowledge.context_for(owner_id, recent_customer)
    links = await db.list_links(owner_id)
    return await ai.decide(tenant, hist, kb, is_full, links, images, playbook=knowledge.read_playbook(),
                           customer_name=customer_name)


async def escalate(bot: Bot, tenant, chat_id: int, p: Pending, reason: str, bot_reply: str = "") -> None:
    """Ping staff. Without bot_reply the customer's message is left unread (nothing was sent);
    with bot_reply (handoff) the bot already answered and staff only need to act."""
    owner_id = tenant["owner_id"]
    question = "\n".join(p.texts)[-1500:]
    esc_id = await db.add_escalation(owner_id, chat_id, p.connection_id, p.message_ids[-1], question, reason)

    c = p.customer
    name = html.escape(c.full_name if c else str(chat_id))
    who = f'<a href="tg://user?id={chat_id}">{name}</a>'
    if c and c.username:
        who += f" (@{html.escape(c.username)})"
    if bot_reply:
        text = (
            f"🟡 <b>Javob berdim, lekin siz ham qarang</b>\n"
            f"👤 {who}\n"
            f"💬 {html.escape(question)}\n"
            f"🤖 Bot: {html.escape(bot_reply[:700])}\n"
            f"📌 Kerak: {html.escape(reason)}\n\n"
            f"<i>Shu xabarga reply qilsangiz, javobingiz mijozga ketadi.</i>"
        )
    else:
        text = (
            f"🔔 <b>Javob bera olmadim</b>\n"
            f"👤 {who}\n"
            f"💬 {html.escape(question)}\n"
            f"📝 Sabab: {html.escape(reason)}\n\n"
            f"<i>Xabar o'qilmagan holda qoldi. Shu xabarga reply qilib javob yozsangiz, uni mijozga "
            f"yuboraman.</i>"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✋ Chatni o'zim olaman", callback_data=f"take:{owner_id}:{chat_id}"),
        InlineKeyboardButton(text="🤖 Bot davom etsin", callback_data=f"release:{owner_id}:{chat_id}"),
    ]])
    target = config.NOTIFY_CHAT_ID or tenant["owner_chat_id"] or owner_id
    sent = await bot.send_message(target, text, parse_mode="HTML", reply_markup=kb)
    await db.set_escalation_notify(esc_id, target, sent.message_id)
    if not config.NOTIFY_CHAT_ID:  # no staff group: every admin gets their own copy to reply to
        for admin in config.ADMIN_IDS - {target, owner_id}:
            try:
                copy = await bot.send_message(admin, text, parse_mode="HTML", reply_markup=kb)
                await db.add_alert_message(esc_id, admin, copy.message_id)
            except Exception as e:  # admin has not opened the bot yet
                log.warning("Could not alert admin %s: %s", admin, e)


def split(text: str, limit: int = 4000) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


async def warm_cache(owner_id: int) -> None:
    """Refresh the 1-hour prompt cache with the exact system prompt real answers use."""
    tenant = await db.get_tenant(owner_id)
    if not tenant or not tenant["is_enabled"] or tenant["paused"]:
        return
    kb, is_full = await knowledge.context_for(owner_id, "")
    if not is_full:
        return  # retrieval mode: the prompt differs per question, nothing stable to keep warm
    links = await db.list_links(owner_id)
    await ai.warm(ai.build_system(tenant, kb, True, links, knowledge.read_playbook()))


def _warm_window_open() -> bool:
    if not config.WARM_HOURS:
        return False
    import datetime as dt
    start, end = (int(x) for x in config.WARM_HOURS.split("-"))
    hour = dt.datetime.now(dt.timezone(dt.timedelta(hours=5))).hour  # Tashkent, no DST
    return start <= hour < end


async def keep_cache_warm(owner_id: int) -> None:
    """While the cache from a real answer is still alive (< 60 min) and it is working hours,
    re-read it just before it expires. Never starts a cache on its own: if nobody wrote,
    nothing is spent."""
    import time
    while True:
        await asyncio.sleep(60)
        try:
            idle = time.monotonic() - ai.last_call_at
            if ai.last_call_at and 50 * 60 <= idle < 58 * 60 and _warm_window_open():
                await warm_cache(owner_id)
                log.info("Prompt cache kept warm")
        except Exception as e:
            log.warning("Cache warm-up failed: %s", e)
