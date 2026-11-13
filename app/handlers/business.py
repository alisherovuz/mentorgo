"""Updates coming from connected Telegram Business accounts."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from .. import config, db, engine

log = logging.getLogger(__name__)
router = Router(name="business")


def _rights(conn: BusinessConnection) -> tuple[bool, bool]:
    r = conn.rights
    can_reply = bool(r.can_reply) if r else bool(getattr(conn, "can_reply", False))
    can_read = bool(r.can_read_messages) if r else False
    return can_reply, can_read


async def _store(conn: BusinessConnection):
    can_reply, can_read = _rights(conn)
    return await db.save_connection(conn.user.id, conn.user_chat_id, conn.id,
                                    conn.is_enabled, can_reply, can_read)


@router.business_connection()
async def on_connection(conn: BusinessConnection, bot: Bot) -> None:
    if config.OWNER_ID and conn.user.id != config.OWNER_ID:
        log.info("Ignoring connection from non-allowed user %s", conn.user.id)
        return
    tenant = await _store(conn)
    if not conn.is_enabled:
        text = "🔌 Bot akkauntingizdan uzildi. Mijozlarga endi javob bermayman."
    elif not tenant["can_reply"]:
        text = ("⚠️ Ulandim, lekin menga <b>xabarlarga javob berish</b> ruxsati berilmagan.\n"
                "Sozlamalar → Telegram Business → Chatbotlar → ruxsatlarni yoqing.")
    else:
        text = "✅ Akkauntingizga ulandim! Mijozlar yozsa, javob beraman."
        if not tenant["can_read"]:
            text += ("\n\n💡 <b>Xabarlarni o'qilgan deb belgilash</b> ruxsatini ham yoqing: shunda men javob "
                     "bergan xabarlar o'qilgan bo'ladi, javob bera olmaganlarim esa o'qilmagan qoladi.")
        if not tenant["business_name"]:
            text += "\n\nKeyingi qadam: /setup"
    try:
        await bot.send_message(conn.user_chat_id, text, parse_mode="HTML")
    except Exception as e:  # owner may never have opened the bot chat
        log.warning("Could not notify owner %s: %s", conn.user.id, e)


async def _tenant_for(message: Message, bot: Bot):
    tenant = await db.tenant_by_connection(message.business_connection_id)
    if tenant is None:  # missed the connection update (e.g. bot was offline)
        conn = await bot.get_business_connection(message.business_connection_id)
        if config.OWNER_ID and conn.user.id != config.OWNER_ID:
            return None
        tenant = await _store(conn)
    return tenant


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    tenant = await _tenant_for(message, bot)
    if tenant is None or not tenant["is_enabled"]:
        return
    owner_id = tenant["owner_id"]
    chat_id = message.chat.id

    if message.from_user and message.from_user.id == owner_id:
        if message.sender_business_bot:
            return  # our own reply echoing back
        # The owner typed by hand: remember it as context and step aside in this chat.
        text, _ = engine.describe(message)
        await db.add_message(owner_id, chat_id, "assistant", "owner", text)
        await db.set_human(owner_id, chat_id, config.HUMAN_TAKEOVER_MINUTES)
        return

    if message.from_user and message.from_user.is_bot:
        return
    if tenant["paused"]:
        return
    if await db.human_active(owner_id, chat_id):
        text, _ = engine.describe(message)
        await db.add_message(owner_id, chat_id, "user", "customer", text)
        return

    await engine.enqueue(bot, owner_id, message)
