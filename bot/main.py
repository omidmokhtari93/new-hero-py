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
    WEBHOOK_URL,
)
from bot.db import init_db
from bot.logging_setup import setup_logging
from bot.telegram_bot import build_telegram_app

setup_logging()
log = logging.getLogger(__name__)

tg_app = build_telegram_app()


def _use_webhook() -> bool:
    return bool(WEBHOOK_URL)


def _telegram_webhook_url() -> str:
    return f"{WEBHOOK_URL}/telegram/webhook"


def _log_startup() -> None:
    log.info("=== vpn-bot starting ===")
    log.info("plans loaded: %s", [p.id for p in PLANS])
    log.info("servers loaded: %s", [(s.id, s.title) for s in SERVERS])
    log.info("telegram mode=%s", "webhook" if _use_webhook() else "polling")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _log_startup()
    init_db()

    async with tg_app:
        await tg_app.start()

        if _use_webhook():
            url = _telegram_webhook_url()
            await tg_app.bot.set_webhook(url, drop_pending_updates=True)
            log.info("telegram webhook set url=%s", url)
        else:
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

        if not _use_webhook():
            await tg_app.updater.stop()


app = FastAPI(title="vpn-bot", lifespan=lifespan)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not _use_webhook():
        return JSONResponse({"ok": False}, status_code=404)

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    log.debug("webhook update_id=%s", update.update_id)
    await tg_app.process_update(update)
    return {"ok": True}


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
