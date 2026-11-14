# MentorGO AI menejer

"MentorGO Manager" Telegram Business akkauntiga ulanadigan AI yordamchi. Mijozlarga menejer uslubida javob beradi; odam kerak bo'lgan holatlarda (to'lov, invoys, sessiya band qilish, shikoyat) xabarni o'qilmagan qoldirib, xodimlarga bildirishnoma yuboradi.

## Tuzilish

```
app/                 bot kodi (aiogram 3 + Claude + Postgres)
knowledge/           bilim bazasi: 00_playbook.md (ish qoidalari) va 01..06 fayllar
tests/e2e_test.py    oqim testi (soxta Telegram va Claude, haqiqiy Postgres)
tests/eval.py        haqiqiy savollar ustida Claude bilan baholash
```

Bilimni yangilash: `knowledge/` dagi faylni tahrirlab push qiling (deploy qayta yuklaydi) yoki botga `/reload` yozing.

## O'zgaruvchilar

| Nomi | Ma'nosi |
|---|---|
| `BOT_TOKEN` | @BotFather tokeni (Business Mode yoqilgan) |
| `ANTHROPIC_API_KEY` | Claude API kaliti |
| `DATABASE_URL` | Postgres manzili |
| `OWNER_ID` | MentorGO Manager akkauntining Telegram ID si: faqat shu akkaunt ulay oladi |
| `ADMIN_IDS` | ixtiyoriy: qo'shimcha adminlarning Telegram ID lari (vergul bilan). Ular buyruqlarni ishlata oladi va bildirishnomalarni oladi |
| `NOTIFY_CHAT_ID` | ixtiyoriy: xodimlar guruhi; bildirishnomalar shu yerga boradi |
| `CLAUDE_MODEL` | default `claude-sonnet-5-5` |

## Ulash

Telegram → Sozlamalar → Telegram Business → Chatbotlar → @mentorgoai. Ruxsatlar: xabarlarga javob berish va xabarlarni o'qish.

## Bot buyruqlari (egasi uchun)

`/status`, `/test` (mijoz bo'lib sinash), `/sources`, `/add matn`, `/url havola`, `/reload`, `/pause`, `/resume`, `/stats`, `/link Mavzu | havola | izoh`.
