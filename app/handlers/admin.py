"""The business owner's control panel: the private chat with the bot."""
from __future__ import annotations

import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from .. import config, db, engine, knowledge

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private")

HELP = """<b>🤖 AI Support — Telegram Business uchun</b>

Mijozlaringizga sizning akkauntingizdan, sizning ma'lumotlaringiz asosida javob beraman. Javob bera olmasam, xabarni o'qilmagan qoldiraman va sizga shu yerda xabar beraman.

<b>1. Ulash</b> (Telegram Premium kerak)
Sozlamalar → Telegram Business → Chatbotlar → shu botni qo'shing. Ruxsatlar: <i>javob berish</i> va <i>xabarlarni o'qish</i>.

<b>2. Biznes profili</b>
/name Kompaniya nomi
/about Nima qilasiz, kimlar uchun (bir necha qator bo'lishi mumkin)
/tone Ohang, masalan: samimiy, "siz"lab, qisqa
/cta Qiziqqan mijozni qayerga yo'naltirish: ro'yxatdan o'tish havolasi, telefon...
/sales on|off — sotuvchi rejimi (follow-up savollar)

<b>3. Bilim bazasi</b>
Fayl yuboring: PDF, DOCX, TXT, MD, CSV
/add matn — narxlar, FAQ, jadval...
/url https://sayt.uz — sahifani o'qib olaman
/sources — ro'yxat, /del 3 — o'chirish

<b>4. Yo'naltirishlar</b>
/link Erasmus | https://t.me/erasmus_community | Erasmus haqida so'ralsa
/links — ro'yxat, /dellink 2 — o'chirish

<b>5. Boshqaruv</b>
/reload — knowledge/ papkasidagi fayllarni qayta yuklash
/test — o'zingiz mijoz bo'lib sinab ko'ring
/pause, /resume — botni to'xtatish/yoqish
/stats — 7 kunlik statistika
/status — holat"""


async def _allowed(message: Message) -> bool:
    if config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        await message.answer("Assalomu alaykum! Bu MentorGO jamoasining ichki boti. Savollaringiz bo'lsa "
                             "@mentorgouz kanalidagi manzillarga yozing.")
        return False
    return True


async def _tenant(message: Message):
    return await db.ensure_tenant(message.from_user.id, message.chat.id)


def _status_text(t) -> str:
    if not t["connection_id"] or not t["is_enabled"]:
        conn = "❌ ulanmagan (Sozlamalar → Telegram Business → Chatbotlar)"
    elif not t["can_reply"]:
        conn = "⚠️ ulangan, lekin javob berish ruxsati yo'q"
    else:
        conn = "✅ ulangan" + ("" if t["can_read"] else " (o'qish ruxsati yo'q)")
    return (
        f"<b>Holat</b>\n"
        f"Akkaunt: {conn}\n"
        f"Bot: {'⏸ pauza' if t['paused'] else '▶️ ishlayapti'}\n"
        f"Nomi: {html.escape(t['business_name'] or '—')}\n"
        f"Tavsif: {'✅' if t['about'] else '—'}  Ohang: {'✅' if t['tone'] else '—'}  CTA: {'✅' if t['cta'] else '—'}\n"
        f"Sotuvchi rejimi: {'yoqilgan' if t['sales_mode'] else 'o‘chirilgan'}\n"
        f"Test rejimi: {'yoqilgan' if t['test_mode'] else 'o‘chirilgan'}"
    )


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    if not await _allowed(message):
        return
    t = await _tenant(message)
    await message.answer(HELP, parse_mode="HTML", disable_web_page_preview=True)
    await message.answer(_status_text(t), parse_mode="HTML")


@router.message(Command("status", "setup"))
async def status(message: Message) -> None:
    if not await _allowed(message):
        return
    t = await _tenant(message)
    text = _status_text(t)
    if t["about"]:
        text += f"\n\n<b>Tavsif:</b>\n{html.escape(t['about'][:800])}"
    if t["cta"]:
        text += f"\n\n<b>CTA:</b> {html.escape(t['cta'])}"
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


PROFILE_FIELDS = {"name": "business_name", "about": "about", "tone": "tone", "cta": "cta"}


@router.message(Command(*PROFILE_FIELDS))
async def set_profile(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    field = PROFILE_FIELDS[command.command]
    value = (command.args or "").strip()
    if not value:
        await message.answer(f"Qiymatni buyruqdan keyin yozing, masalan:\n<code>/{command.command} ...</code>",
                             parse_mode="HTML")
        return
    await db.set_tenant_field(message.from_user.id, field, value[:4000])
    await message.answer("✅ Saqlandi.")


@router.message(Command("sales"))
async def sales(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    on = (command.args or "").strip().lower() not in {"off", "0", "yo'q", "o'chir"}
    await db.set_tenant_field(message.from_user.id, "sales_mode", on)
    await message.answer("🛍 Sotuvchi rejimi yoqildi." if on else "Sotuvchi rejimi o'chirildi.")


@router.message(Command("pause", "resume"))
async def pause(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    paused = command.command == "pause"
    await db.set_tenant_field(message.from_user.id, "paused", paused)
    await message.answer("⏸ To'xtatildi. Mijozlarga javob bermayman." if paused else "▶️ Yana ishlayapman.")


# ---------- knowledge ----------

@router.message(Command("add"))
async def add_text(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    text = (command.args or "").strip()
    if len(text) < 10:
        await message.answer("Matnni buyruq bilan birga yuboring:\n<code>/add Kurs narxi oyiga 500 000 so'm...</code>",
                             parse_mode="HTML")
        return
    title = text.splitlines()[0][:60]
    sid = await knowledge.add(message.from_user.id, title, "text", text)
    await message.answer(f"📚 Qo'shildi (#{sid}).")


@router.message(Command("url"))
async def add_url(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    url = (command.args or "").strip()
    if not url.startswith(("http://", "https://")):
        await message.answer("Masalan: <code>/url https://mentorgo.uz</code>", parse_mode="HTML")
        return
    try:
        title, text = await knowledge.fetch_url(url)
        sid = await knowledge.add(message.from_user.id, f"{title} ({url})", "url", text)
    except Exception as e:
        await message.answer(f"Sahifani o'qiy olmadim: {html.escape(str(e))[:200]}", parse_mode="HTML")
        return
    await message.answer(f"🌐 Qo'shildi (#{sid}): {html.escape(title)} — {len(text):,} belgi", parse_mode="HTML")


@router.message(F.document)
async def add_document(message: Message, bot: Bot) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    doc = message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("Fayl 20 MB dan katta. Kichikroq qismlarga bo'lib yuboring.")
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        text = knowledge.extract_text(doc.file_name or "file.txt", buf.getvalue())
        sid = await knowledge.add(message.from_user.id, doc.file_name or "Fayl", "file", text)
    except knowledge.ExtractError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("Document parse failed")
        await message.answer("Faylni o'qishda xatolik bo'ldi.")
        return
    await message.answer(f"📄 Qo'shildi (#{sid}): {html.escape(doc.file_name or '')} — {len(text):,} belgi",
                         parse_mode="HTML")


@router.message(Command("reload"))
async def reload_knowledge(message: Message) -> None:
    if not await _allowed(message):
        return
    n = await knowledge.seed(message.from_user.id)
    pb = "bor" if knowledge.read_playbook() else "yo'q"
    await message.answer(f"🔄 Fayllardan qayta yuklandi: {n} ta bilim fayli, playbook: {pb}.")


@router.message(Command("sources"))
async def sources(message: Message) -> None:
    if not await _allowed(message):
        return
    rows = await db.list_sources(message.from_user.id)
    if not rows:
        await message.answer("Bilim bazasi bo'sh. Fayl yuboring yoki /add, /url dan foydalaning.")
        return
    icons = {"file": "📄", "url": "🌐", "text": "📝", "learned": "🧠", "seed": "📦"}
    lines = [f"{icons.get(r['kind'], '•')} #{r['id']} {html.escape(r['title'][:70])} ({r['size']:,})" for r in rows]
    total = sum(r["size"] for r in rows)
    lines.append(f"\nJami: {total:,} belgi. O'chirish: /del ID")
    for part in engine.split("\n".join(lines)):
        await message.answer(part, parse_mode="HTML")


@router.message(Command("del"))
async def delete_source(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    try:
        ok = await db.delete_source(message.from_user.id, int((command.args or "").strip().lstrip("#")))
    except ValueError:
        ok = False
    await message.answer("🗑 O'chirildi." if ok else "Bunday manba topilmadi. /sources")


# ---------- referral links ----------

@router.message(Command("link"))
async def add_link(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    await _tenant(message)
    parts = [p.strip() for p in (command.args or "").split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Format: <code>/link Mavzu | havola | qachon yo'naltirish (ixtiyoriy)</code>\n"
            "Masalan: <code>/link Erasmus | https://t.me/erasmus_uz | Erasmus haqida so'ralsa</code>",
            parse_mode="HTML", disable_web_page_preview=True)
        return
    lid = await db.add_link(message.from_user.id, parts[0], parts[1], parts[2] if len(parts) > 2 else "")
    await message.answer(f"🔗 Qo'shildi (#{lid}).")


@router.message(Command("links"))
async def links(message: Message) -> None:
    if not await _allowed(message):
        return
    rows = await db.list_links(message.from_user.id)
    if not rows:
        await message.answer("Yo'naltirishlar yo'q. /link bilan qo'shing.")
        return
    text = "\n".join(f"#{r['id']} {html.escape(r['topic'])} → {html.escape(r['url'])}" for r in rows)
    await message.answer(text + "\n\nO'chirish: /dellink ID", parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("dellink"))
async def delete_link(message: Message, command: CommandObject) -> None:
    if not await _allowed(message):
        return
    try:
        ok = await db.delete_link(message.from_user.id, int((command.args or "").strip().lstrip("#")))
    except ValueError:
        ok = False
    await message.answer("🗑 O'chirildi." if ok else "Topilmadi. /links")


# ---------- stats & test ----------

@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not await _allowed(message):
        return
    s = await db.stats(message.from_user.id)
    await message.answer(
        f"📊 <b>Oxirgi 7 kun</b>\n"
        f"Mijozlar: {s['customers']}\n"
        f"Bot javoblari: {s['bot_replies']}\n"
        f"Sizga yo'naltirilgan: {s['escalations']} (ochiq: {s['open_escalations']})\n"
        f"Bilim manbalari: {s['sources']}",
        parse_mode="HTML")


@router.message(Command("test"))
async def test_mode(message: Message) -> None:
    if not await _allowed(message):
        return
    t = await _tenant(message)
    on = not t["test_mode"]
    await db.set_tenant_field(message.from_user.id, "test_mode", on)
    await db.clear_history(message.from_user.id, message.from_user.id)
    if on:
        await message.answer("🧪 Test rejimi yoqildi. Endi mijoz bo'lib yozing, men qanday javob berishimni "
                             "ko'rasiz. Chiqish: /test")
    else:
        await message.answer("Test rejimi o'chirildi.")


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message) -> None:
    if not await _allowed(message):
        return
    t = await _tenant(message)
    if not t["test_mode"]:
        await message.answer("Bilim qo'shish: fayl yuboring yoki <code>/add matn</code>. "
                             "Sinab ko'rish: /test. Barcha buyruqlar: /help", parse_mode="HTML")
        return
    owner_id = message.from_user.id
    await db.add_message(owner_id, owner_id, "user", "customer", message.text)
    await message.bot.send_chat_action(message.chat.id, "typing")
    decision = await engine.think(t, owner_id)
    if decision.action in ("reply", "handoff"):
        await db.add_message(owner_id, owner_id, "assistant", "bot", decision.reply)
        for part in engine.split(decision.reply):
            await message.answer(part)
        if decision.action == "handoff":
            await message.answer(f"🟡 <i>(Xodimlarga ham bildirishnoma borardi.)</i>\nKerak: {html.escape(decision.reason)}",
                                 parse_mode="HTML")
    else:
        await message.answer(f"🔔 <i>(Haqiqiy chatda javob bermasdim, xabar o'qilmagan qolardi va sizga "
                             f"bildirishnoma kelardi.)</i>\nSabab: {html.escape(decision.reason)}",
                             parse_mode="HTML")
