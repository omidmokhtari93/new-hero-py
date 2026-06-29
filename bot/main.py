import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update

from bot.config import (
    PLANS,
    SERVERS,
)
from bot.db import init_db
from bot.logging_setup import setup_logging
from bot.telegram_bot import build_telegram_app

setup_logging()
log = logging.getLogger(__name__)

tg_app = build_telegram_app()


def _log_startup() -> None:
    log.info("=== vpn-bot starting ===")
    log.info("plans loaded: %s", [p.id for p in PLANS])
    log.info("servers loaded: %s", [(s.id, s.title) for s in SERVERS])
    log.info("telegram mode=%s", "polling")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _log_startup()
    init_db()

    async with tg_app:
        await tg_app.start()

        info = await tg_app.bot.get_webhook_info()
        if info.url:
            log.warning("removing stale webhook url=%s", info.url)
            await tg_app.bot.delete_webhook(drop_pending_updates=True)

        await tg_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        log.info("telegram polling started (only ONE instance must run this bot token)")

        me = await tg_app.bot.get_me()
        log.info("bot ready @%s id=%s", me.username, me.id)

        yield

        await tg_app.updater.stop()


app = FastAPI(title="vpn-bot", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        loop="asyncio",
    )
