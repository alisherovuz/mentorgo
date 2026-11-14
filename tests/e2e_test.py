"""End-to-end: real Dispatcher + real Postgres, fake Telegram session and fake Claude."""
import asyncio, os, sys, time, io
from types import SimpleNamespace

import tempfile
KDIR = tempfile.mkdtemp()
open(os.path.join(KDIR, "00_playbook.md"), "w").write("Siz MentorGO menejerisiz. PLAYBOOK_MARKER")
open(os.path.join(KDIR, "kurslar.md"), "w").write("DAAD mentorlik kursi magistratura uchun. SEED_MARKER")
os.environ.update(BOT_TOKEN="123:ABC", ANTHROPIC_API_KEY="x", DEBOUNCE_SECONDS="0.3", OWNER_ID="1001", ADMIN_IDS="3003", KNOWLEDGE_DIR=KDIR,
                  DATABASE_URL=os.getenv("TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/support_test"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update
from app import ai, db, config
from app import knowledge as kn
from app.handlers import admin, business, staff

OWNER, CUSTOMER, CONN = 1001, 2002, "bc_1"
calls = []
msg_counter = [500]


class FakeSession(BaseSession):
    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        calls.append((name, method))
        if name == "SendMessage":
            msg_counter[0] += 1
            return method.__returning__.model_validate({
                "message_id": msg_counter[0], "date": int(time.time()),
                "chat": {"id": method.chat_id, "type": "private"}, "text": method.text})
        if name == "GetFile":
            return method.__returning__.model_validate({"file_id": method.file_id, "file_unique_id": "u", "file_path": "doc.txt"})
        if name == "GetBusinessConnection":
            return method.__returning__.model_validate(conn_payload())
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield "Kurs narxi oyiga 600 000 so'm. Darslar dushanba va chorshanba 18:00 da.".encode()

    async def close(self): pass


def conn_payload(enabled=True):
    return {"id": CONN, "user": {"id": OWNER, "is_bot": False, "first_name": "Nurbek"},
            "user_chat_id": OWNER, "date": int(time.time()), "is_enabled": enabled,
            "rights": {"can_reply": True, "can_read_messages": True}}


seen_prompts = []


class FakeMessages:
    async def create(self, **kw):
        seen_prompts.append(kw)
        last = kw["messages"][-1]["content"][-1]["text"].lower()
        system = " ".join(b["text"] for b in kw["system"])
        if "qo'shilmoqchiman" in last:
            inp = {"action": "handoff", "reply": "Ajoyib! Jamoamiz tasdiqlab invoys yuboradi.", "reason": "Tayyor mijoz: Standard"}
        elif "pul" in last:
            inp = {"action": "escalate", "reason": "To'lov qaytarish so'ralmoqda"}
        elif "erasmus" in last:
            inp = {"action": "reply", "reply": "Erasmus+ Yevropa dasturi. Hamjamiyat: https://t.me/erasmus_uz"}
        elif "narx" in last and "600 000" in system:
            inp = {"action": "reply", "reply": "Kurs narxi oyiga 600 000 so'm. Qaysi darajadasiz?"}
        else:
            inp = {"action": "escalate", "reason": "Ma'lumot yo'q"}
        import json as _json
        full = {"action": inp["action"], "reply": inp.get("reply", ""), "reason": inp.get("reason", "")}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=_json.dumps(full))],
                               stop_reason="end_turn", usage=SimpleNamespace(output_tokens=10))


ai._client = SimpleNamespace(messages=FakeMessages())


def upd(uid, **kw):
    return Update.model_validate({"update_id": uid, **kw})


def msg(mid, frm, chat, text=None, business=False, reply_to=None, **extra):
    m = {"message_id": mid, "date": int(time.time()),
         "chat": {"id": chat, "type": "private", "first_name": "X"},
         "from": {"id": frm, "is_bot": False, "first_name": "Ali" if frm == CUSTOMER else "Nurbek", "username": "ali_uz"}}
    if text is not None:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if business:
        m["business_connection_id"] = CONN
    if reply_to:
        m["reply_to_message"] = reply_to
    m.update(extra)
    return m


def names():
    return [c[0] for c in calls]


def sent_to(chat):
    return [c[1].text for c in calls if c[0] == "SendMessage" and c[1].chat_id == chat]


async def main():
    await db.init()
    async with db.pool.acquire() as con:
        await con.execute("TRUNCATE tenants, sources, chunks, links, chats, messages, escalations RESTART IDENTITY CASCADE")
    bot = Bot("123:ABC", session=FakeSession())
    dp = Dispatcher(); dp.include_routers(business.router, staff.router, admin.router)
    await kn.seed(OWNER)
    print("update types:", dp.resolve_used_update_types())
    u = iter(range(1, 10000))
    feed = lambda x: dp.feed_update(bot, x)
    ok = lambda cond, label: print(("PASS " if cond else "FAIL ") + label) or cond
    results = []

    # owner onboarding
    await feed(upd(next(u), message=msg(1, OWNER, OWNER, "/start")))
    await feed(upd(next(u), message=msg(2, OWNER, OWNER, "/name EduCenter")))
    await feed(upd(next(u), message=msg(3, OWNER, OWNER, "/about IELTS va grantlar bo'yicha o'quv markaz\nToshkent")))
    await feed(upd(next(u), message=msg(4, OWNER, OWNER, "/link Erasmus | https://t.me/erasmus_uz | Erasmus so'ralsa")))
    await feed(upd(next(u), message=msg(5, OWNER, OWNER, None, document={"file_id": "F1", "file_unique_id": "U1", "file_name": "narxlar.txt", "file_size": 100})))
    t = await db.get_tenant(OWNER)
    results.append(ok(t["business_name"] == "EduCenter" and "Toshkent" in t["about"], "profile saved (multiline about)"))
    results.append(ok(any("Qo'shildi" in s for s in sent_to(OWNER)), "document ingested"))

    # connect business account
    await feed(upd(next(u), business_connection=conn_payload()))
    t = await db.get_tenant(OWNER)
    results.append(ok(t["connection_id"] == CONN and t["can_reply"] and t["can_read"], "business connection stored"))

    # returning customer: past chat (before the bot) is shown to the model
    await db.replace_past_messages([("ali_uz", 0, "2026-08-01 10:00:00", "customer", "Standard paketni oldim"),
                                    ("ali_uz", 1, "2026-08-01 10:05:00", "manager", "Qabul qildik, guruh havolasi botda")])

    # customer: two quick messages -> one answer, then read
    calls.clear()
    await feed(upd(next(u), business_message=msg(10, CUSTOMER, CUSTOMER, "Assalomu alaykum", business=True)))
    await feed(upd(next(u), business_message=msg(11, CUSTOMER, CUSTOMER, "kurs narxi qancha?", business=True)))
    await asyncio.sleep(0.8)
    replies = [c[1] for c in calls if c[0] == "SendMessage" and c[1].business_connection_id == CONN]
    results.append(ok(len(replies) == 1 and "600 000" in replies[0].text, "debounced single grounded reply"))
    reads = [c[1] for c in calls if c[0] == "ReadBusinessMessage"]
    results.append(ok(len(reads) == 1 and reads[0].message_id == 11, "marked last message read"))
    results.append(ok("SendChatAction" in names(), "typing indicator"))
    sys_text = seen_prompts[-1]["system"][0]["text"]
    results.append(ok("erasmus_uz" in sys_text and "SALES MODE" in sys_text and "EduCenter" in sys_text, "prompt has links, sales mode, profile"))
    results.append(ok("PLAYBOOK_MARKER" in sys_text and "SEED_MARKER" in sys_text, "playbook and seeded knowledge in prompt"))
    from app import engine as eng
    real_sys = seen_prompts[-1]["system"][0]["text"]
    await eng.warm_cache(OWNER)
    results.append(ok(seen_prompts[-1]["system"][0]["text"] == real_sys and seen_prompts[-1]["max_tokens"] <= 16,
                      "cache warm-up sends the identical cached prompt"))
    first_turns = seen_prompts[-2]["messages"]
    results.append(ok("[eski yozishma, 2026-08-01] Standard paketni oldim" in first_turns[0]["content"][0]["text"], "past chat history included"))
    results.append(ok(first_turns[-1]["role"] == "user" and "kurs narxi" in first_turns[-1]["content"][-1]["text"], "current message last, roles alternate"))

    # echo of our own reply must be ignored
    calls.clear()
    await feed(upd(next(u), business_message=msg(12, OWNER, CUSTOMER, "Kurs narxi...", business=True, sender_business_bot={"id": 99, "is_bot": True, "first_name": "B"})))
    await asyncio.sleep(0.5)
    results.append(ok(not await db.human_active(OWNER, CUSTOMER) and not calls, "own echo ignored"))

    # escalation: unread + owner notified
    calls.clear()
    await feed(upd(next(u), business_message=msg(13, CUSTOMER, CUSTOMER, "pulimni qaytaring", business=True)))
    await asyncio.sleep(0.8)
    results.append(ok("ReadBusinessMessage" not in names(), "escalated message left unread"))
    notif = [c[1] for c in calls if c[0] == "SendMessage" and c[1].chat_id == OWNER]
    results.append(ok(len(notif) == 1 and "Javob bera olmadim" in notif[0].text and notif[0].reply_markup is not None, "owner notified with buttons"))
    results.append(ok(not [c for c in calls if c[0] == "SendMessage" and c[1].business_connection_id], "nothing sent to customer"))
    notify_id = await db.pool.fetchval("SELECT notify_msg_id FROM escalations ORDER BY id DESC LIMIT 1")

    # owner replies to notification -> sent to customer, read, learned
    calls.clear()
    reply_to = msg(notify_id, 99, OWNER, "🔔 Javob bera olmadim")
    reply_to["from"]["is_bot"] = True
    await feed(upd(next(u), message=msg(20, OWNER, OWNER, "To'lov 3 ish kunida qaytariladi", reply_to=reply_to)))
    sent = [c[1] for c in calls if c[0] == "SendMessage" and c[1].business_connection_id == CONN]
    results.append(ok(sent and sent[0].chat_id == CUSTOMER, "owner answer delivered to customer"))
    results.append(ok(any(c[0] == "ReadBusinessMessage" and c[1].message_id == 13 for c in calls), "escalated msg now read"))
    srcs = await db.list_sources(OWNER)
    results.append(ok(not any(s["kind"] == "learned" for s in srcs), "not learned without consent"))
    esc_id = (await db.escalation_by_notify(OWNER, notify_id))["id"]
    await feed(upd(next(u), callback_query={"id": "cb0", "chat_instance": "c", "data": f"learn:{esc_id}",
        "message": {"message_id": 999, "date": int(time.time()), "chat": {"id": OWNER, "type": "private"}, "text": "x"},
        "from": {"id": OWNER, "is_bot": False, "first_name": "N"}}))
    srcs = await db.list_sources(OWNER)
    results.append(ok(any(s["kind"] == "learned" for s in srcs), "learned after pressing button"))

    # handoff: customer gets a reply, message is read, staff get a yellow alert
    calls.clear()
    await feed(upd(next(u), business_message=msg(19, CUSTOMER, CUSTOMER, "Standardga qo'shilmoqchiman", business=True)))
    await asyncio.sleep(0.8)
    to_cust = [c[1] for c in calls if c[0] == "SendMessage" and c[1].business_connection_id == CONN]
    to_owner = [c[1] for c in calls if c[0] == "SendMessage" and c[1].chat_id == OWNER]
    results.append(ok(to_cust and "invoys" in to_cust[0].text and any(c[0] == "ReadBusinessMessage" for c in calls), "handoff replies and marks read"))
    results.append(ok(to_owner and "Javob berdim" in to_owner[0].text and "Tayyor mijoz" in to_owner[0].text, "handoff alerts staff"))

    # owner types by hand -> bot steps aside
    calls.clear()
    await feed(upd(next(u), business_message=msg(21, OWNER, CUSTOMER, "Men o'zim yozaman", business=True)))
    await feed(upd(next(u), business_message=msg(22, CUSTOMER, CUSTOMER, "kurs narxi?", business=True)))
    await asyncio.sleep(0.6)
    results.append(ok(not [c for c in calls if c[0] == "SendMessage"], "bot silent during human takeover"))

    # release via button -> answers again
    await feed(upd(next(u), callback_query={"id": "cb1", "chat_instance": "c", "data": f"release:{OWNER}:{CUSTOMER}",
                                             "message": {"message_id": 998, "date": int(time.time()), "chat": {"id": OWNER, "type": "private"}, "text": "x"},
                                             "from": {"id": OWNER, "is_bot": False, "first_name": "N"}}))
    calls.clear()
    await feed(upd(next(u), business_message=msg(23, CUSTOMER, CUSTOMER, "Erasmus haqida aytib bering", business=True)))
    await asyncio.sleep(0.6)
    results.append(ok(any("erasmus_uz" in (c[1].text or "") for c in calls if c[0] == "SendMessage"), "referral after release"))

    # second admin: manages the owner's knowledge, gets alerts and can answer them
    ADMIN = 3003
    calls.clear()
    await feed(upd(next(u), message=msg(50, ADMIN, ADMIN, "/add Erasmus kursi 1-dekabrda ochiladi, ADMIN_MARKER")))
    srcs = await db.list_sources(OWNER)
    results.append(ok(any("Erasmus kursi" in s_["title"] for s_ in srcs) and await db.get_tenant(ADMIN) is None,
                      "admin /add goes to the MentorGO knowledge base"))
    t_ = await db.get_tenant(OWNER)
    results.append(ok(t_["owner_chat_id"] == OWNER, "admin does not take over owner's alert chat"))
    calls.clear()
    await feed(upd(next(u), business_message=msg(51, CUSTOMER, CUSTOMER, "pulimni qaytaring iltimos", business=True)))
    await asyncio.sleep(0.8)
    admin_alert = [c for c in calls if c[0] == "SendMessage" and c[1].chat_id == ADMIN]
    results.append(ok(len(admin_alert) == 1 and "Javob bera olmadim" in admin_alert[0][1].text, "admin gets a copy of the alert"))
    admin_copy_id = await db.pool.fetchval("SELECT msg_id FROM alert_messages ORDER BY escalation_id DESC LIMIT 1")
    calls.clear()
    rt = msg(admin_copy_id, 99, ADMIN, "🔔 Javob bera olmadim"); rt["from"]["is_bot"] = True
    await feed(upd(next(u), message=msg(52, ADMIN, ADMIN, "Pulni qaytarish bo'yicha hozir qo'ng'iroq qilamiz", reply_to=rt)))
    sent = [c[1] for c in calls if c[0] == "SendMessage" and c[1].business_connection_id == CONN]
    results.append(ok(sent and sent[0].chat_id == CUSTOMER and "qo'ng'iroq" in sent[0].text, "admin reply to his copy reaches the customer"))

    # test mode
    calls.clear()
    await feed(upd(next(u), message=msg(30, OWNER, OWNER, "/test")))
    await feed(upd(next(u), message=msg(31, OWNER, OWNER, "narxi qancha?")))
    results.append(ok(any("600 000" in t for t in sent_to(OWNER)), "test mode simulates answer"))

    # pause
    await feed(upd(next(u), message=msg(32, OWNER, OWNER, "/pause")))
    calls.clear()
    await feed(upd(next(u), business_message=msg(33, CUSTOMER, CUSTOMER, "narx?", business=True)))
    await asyncio.sleep(0.5)
    results.append(ok(not calls, "paused bot stays silent"))

    # retrieval mode for big knowledge base
    config.FULL_CONTEXT_CHARS = 10
    from app import knowledge
    kb, full = await knowledge.context_for(OWNER, "kurslarimiz narxlari qancha")
    results.append(ok(not full and "600 000" in kb, "search retrieval finds price with suffixed words"))

    # stranger cannot connect or use admin
    calls.clear()
    await feed(upd(next(u), message=msg(40, 7777, 7777, "/start")))
    results.append(ok(any("ichki boti" in (c[1].text or "") for c in calls if c[0] == "SendMessage"), "stranger blocked from admin"))
    stranger = conn_payload(); stranger["id"] = "bc_x"; stranger["user"]["id"] = 7777
    await feed(upd(next(u), business_connection=stranger))
    results.append(ok(await db.get_tenant(7777) is None or not (await db.get_tenant(7777))["connection_id"], "stranger connection ignored"))

    # disconnect
    await feed(upd(next(u), business_connection=conn_payload(enabled=False)))
    results.append(ok(not (await db.get_tenant(OWNER))["is_enabled"], "disconnect handled"))

    print(f"\n{sum(results)}/{len(results)} passed")


asyncio.run(main())
