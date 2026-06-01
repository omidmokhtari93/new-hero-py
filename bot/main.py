import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update

from bot.config import (
    HIDDIFY_BASE_URL,
    NOVINOPAY_MERCHANT_ID,
    PAYMENT_CALLBACK_BASE,
    PLANS,
    PLANS_BY_ID,
)
from bot.db import get_order_by_authority, init_db, mark_failed, mark_paid
from bot.hiddify import create_user, subscription_url
from bot.logging_setup import setup_logging
from bot.payment import verify_payment
from bot.telegram_bot import build_telegram_app

setup_logging()
log = logging.getLogger(__name__)

tg_app = build_telegram_app()


def _use_webhook() -> bool:
    return PAYMENT_CALLBACK_BASE.startswith("https://")


def _telegram_webhook_url() -> str:
    return f"{PAYMENT_CALLBACK_BASE}/telegram/webhook"


def _log_startup() -> None:
    log.info("=== vpn-bot starting ===")
    log.info("plans loaded: %s", [p.id for p in PLANS])
    log.info("hiddify base=%s", HIDDIFY_BASE_URL)
    log.info("payment callback base=%s", PAYMENT_CALLBACK_BASE)
    log.info("telegram mode=%s", "webhook" if _use_webhook() else "polling")
    merchant = NOVINOPAY_MERCHANT_ID
    log.info(
        "novinopay merchant=%s",
        merchant if merchant == "test" else f"{merchant[:8]}...",
    )


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


@app.get("/payment/callback")
async def payment_callback(
    PaymentStatus: str,
    Authority: str,
    InvoiceID: str,
):
    log.info(
        "payment callback PaymentStatus=%s Authority=%s InvoiceID=%s",
        PaymentStatus,
        Authority,
        InvoiceID,
    )

    if PaymentStatus != "OK":
        log.info("payment cancelled or failed at gateway InvoiceID=%s", InvoiceID)
        return HTMLResponse(
            "<h3>پرداخت لغو شد</h3><p>می‌تونی دوباره از ربات /start بزنی.</p>",
        )

    try:
        invoice_order_id = int(InvoiceID)
    except ValueError:
        log.warning("invalid InvoiceID=%s", InvoiceID)
        return HTMLResponse("<h3>شماره فاکتور نامعتبر</h3>", status_code=400)

    order = get_order_by_authority(Authority)
    if not order:
        log.warning(
            "order not found for authority=%s invoice=%s",
            Authority,
            invoice_order_id,
        )
        return HTMLResponse("<h3>سفارش نامعتبر</h3>", status_code=400)

    if order["id"] != invoice_order_id:
        log.warning(
            "invoice mismatch order_id=%s invoice=%s authority=%s",
            order["id"],
            invoice_order_id,
            Authority,
        )
        return HTMLResponse("<h3>سفارش نامعتبر</h3>", status_code=400)

    log.info(
        "order matched id=%s telegram_id=%s status=%s plan=%s amount=%s",
        order["id"],
        order["telegram_id"],
        order["status"],
        order["plan_id"],
        order["amount_rial"],
    )

    if order["status"] == "paid":
        log.info("order %s already paid, skipping", order["id"])
        return HTMLResponse("<h3>این سفارش قبلاً پردازش شده</h3>")

    plan = PLANS_BY_ID.get(order["plan_id"])
    if not plan:
        log.error("unknown plan_id=%s for order %s", order["plan_id"], order["id"])
        mark_failed(order["id"])
        return HTMLResponse("<h3>پلن نامعتبر</h3>", status_code=400)

    try:
        ok = await verify_payment(order["amount_rial"], Authority)
    except Exception:
        log.exception("verify raised for order %s", order["id"])
        return HTMLResponse("<h3>خطا در ارتباط با درگاه</h3>", status_code=502)

    if not ok:
        mark_failed(order["id"])
        await tg_app.bot.send_message(
            order["telegram_id"],
            "پرداخت تایید نشد. اگر مبلغ کسر شده، با پشتیبانی تماس بگیر.",
        )
        return HTMLResponse("<h3>تایید پرداخت ناموفق</h3>")

    try:
        user = await create_user(order["telegram_id"], plan)
        uid = user["uuid"]
        sub = subscription_url(uid)
        mark_paid(order["id"], uid)
        log.info(
            "order %s completed telegram_id=%s hiddify_uuid=%s sub=%s",
            order["id"],
            order["telegram_id"],
            uid,
            sub,
        )

        await tg_app.bot.send_message(
            order["telegram_id"],
            f"✅ پرداخت موفق!\n\n"
            f"پلن: {plan.title}\n"
            f"لینک اشتراک:\n{sub}\n\n"
            "این لینک را در Hiddify یا هر کلاینت سازگار import کن.",
        )
    except Exception as e:
        log.exception("provision failed order=%s", order["id"])
        mark_failed(order["id"])
        await tg_app.bot.send_message(
            order["telegram_id"],
            f"پرداخت OK بود ولی ساخت اکانت خطا داد: {e}\n"
            f"با پشتیبانی تماس بگیر — شماره سفارش: {order['id']}",
        )
        return HTMLResponse(
            f"<h3>خطا در ساخت اکانت</h3><p>{e}</p>",
            status_code=500,
        )

    return HTMLResponse(
        "<h3>پرداخت موفق ✅</h3>"
        "<p>لینک اشتراک در تلگرام برات ارسال شد.</p>",
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        loop="asyncio",
    )
