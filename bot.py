"""Entry point for hosts that start `bot.py` (e.g. LivOps). Same as `python -m app.main`."""
import asyncio

from app.main import main

if __name__ == "__main__":
    asyncio.run(main())
