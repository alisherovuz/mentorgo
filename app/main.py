import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import config, db, engine, history, knowledge
from .handlers import admin, business, staff


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN or not config.ANTHROPIC_API_KEY:
        raise SystemExit("BOT_TOKEN va ANTHROPIC_API_KEY .env faylida bo'lishi kerak")

    await db.init()
    if config.OWNER_ID:
        n = await knowledge.seed(config.OWNER_ID)
        logging.info("Seeded %d knowledge files from %s", n, config.KNOWLEDGE_DIR)
    h = await history.load()
    logging.info("Past chats: %s", "already loaded" if h == -1 else f"{h} loaded")
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_routers(business.router, staff.router, admin.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Yo'riqnoma"),
        BotCommand(command="status", description="Holat va profil"),
        BotCommand(command="sources", description="Bilim bazasi"),
        BotCommand(command="links", description="Yo'naltirishlar"),
        BotCommand(command="test", description="Mijoz bo'lib sinash"),
        BotCommand(command="stats", description="Statistika"),
        BotCommand(command="reload", description="Bilim fayllarini qayta yuklash"),
        BotCommand(command="pause", description="To'xtatish"),
        BotCommand(command="resume", description="Davom ettirish"),
    ])
    if config.OWNER_ID and config.WARM_HOURS:
        asyncio.create_task(engine.keep_cache_warm(config.OWNER_ID))
    me = await bot.get_me()
    logging.info("Started as @%s (business mode: %s)", me.username, getattr(me, "can_connect_to_business", None))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
