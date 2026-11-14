"""Staff answering alerts: in the owner's chat with the bot, or in the staff group (NOTIFY_CHAT_ID)."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import config, db, knowledge

log = logging.getLogger(__name__)
router = Router(name="staff")


def _is_staff_chat(chat_id: int, user_id: int) -> bool:
    if config.NOTIFY_CHAT_ID and chat_id == config.NOTIFY_CHAT_ID:
        return True  # everyone in the private staff group is staff
    return chat_id == user_id and config.is_admin(user_id)


@router.message(F.reply_to_message & F.text & ~F.text.startswith("/"))
async def answer_escalation(message: Message, bot: Bot) -> None:
    if not _is_staff_chat(message.chat.id, message.from_user.id):
        raise SkipHandler()
    esc = await db.escalation_by_notify(message.chat.id, message.reply_to_message.message_id)
    if esc is None:
        raise SkipHandler()

    tenant = await db.get_tenant(esc["owner_id"])
    conn_id = (tenant and tenant["connection_id"]) or esc["connection_id"]
    try:
        await bot.send_message(esc["chat_id"], message.text, business_connection_id=conn_id, parse_mode=None)
    except Exception as e:
        await message.reply(f"Yubora olmadim: {html.escape(str(e))[:200]}", parse_mode="HTML")
        return
    try:
        await bot.read_business_message(conn_id, esc["chat_id"], esc["last_message_id"])
    except Exception:
        pass
    await db.add_message(esc["owner_id"], esc["chat_id"], "assistant", "owner", message.text)
    await db.set_human(esc["owner_id"], esc["chat_id"], None)
    await db.resolve_escalation(esc["id"], message.text)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧠 Bilim bazasiga qo'shish", callback_data=f"learn:{esc['id']}"),
    ]])
    await message.reply(
        "✅ Mijozga yuborildi.\nBu javob boshqa mijozlarga ham to'g'ri bo'lsa (shaxsiy emas), bilim bazasiga "
        "qo'shing: keyingi safar bot o'zi javob beradi.", reply_markup=kb)


@router.callback_query(F.data.startswith("learn:"))
async def learn(cb: CallbackQuery) -> None:
    if not _is_staff_chat(cb.message.chat.id, cb.from_user.id):
        await cb.answer()
        return
    esc = await db.get_escalation(int(cb.data.split(":", 1)[1]))
    if not esc or not esc["answer"]:
        await cb.answer("Topilmadi")
        return
    await knowledge.add(esc["owner_id"], f"Savol-javob: {esc['question'][:50]}", "learned",
                        f"Mijoz savoli: {esc['question']}\nXodim javobi: {esc['answer']}")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Qo'shildi 🧠")


@router.callback_query(F.data.startswith(("take:", "release:")))
async def take_release(cb: CallbackQuery) -> None:
    if not _is_staff_chat(cb.message.chat.id, cb.from_user.id):
        await cb.answer()
        return
    action, owner_id, chat_id = cb.data.split(":")
    if action == "take":
        await db.set_human(int(owner_id), int(chat_id), 24 * 60)
        await cb.answer("Bot bu chatda 24 soat jim turadi.")
    else:
        await db.set_human(int(owner_id), int(chat_id), None)
        await cb.answer("Bot bu chatda yana javob beradi.")
