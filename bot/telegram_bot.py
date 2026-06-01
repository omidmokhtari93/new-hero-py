import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from bot.config import PLANS, TELEGRAM_BOT_TOKEN
from bot.db import create_order, set_authority
from bot.payment import create_payment_link

log = logging.getLogger(__name__)


def _plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(PLANS):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{p.title} — {p.price_rial:,} ریال",
                    callback_data=f"buy:{i}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        q = update.callback_query
        log.info(
            "telegram callback data=%r from=%s msg_id=%s",
            q.data,
            q.from_user.id,
            q.message.message_id if q.message else None,
        )
    elif update.message:
        log.info(
            "telegram message text=%r from=%s",
            update.message.text,
            update.effective_user.id if update.effective_user else None,
        )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram handler error update=%s", update, exc_info=context.error)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log.info("/start from telegram_id=%s username=%s", user.id, user.username)
    if not PLANS:
        await update.message.reply_text("هیچ پلنی تعریف نشده.")
        return
    await update.message.reply_text(
        "سلام! یک پلن انتخاب کن تا لینک پرداخت برات بیاد.\n"
        "بعد از پرداخت موفق، لینک اشتراک VPN همین‌جا ارسال می‌شه.",
        reply_markup=_plans_keyboard(),
    )
    log.info("sent %d plan buttons", len(PLANS))


async def on_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    log.info("on_buy handler data=%r", query.data)

    if not query.data or not query.data.startswith("buy:"):
        await query.answer()
        return

    try:
        plan_index = int(query.data.split(":", 1)[1])
        plan = PLANS[plan_index]
    except (ValueError, IndexError):
        log.warning("invalid buy callback data=%r", query.data)
        await query.answer("پلن نامعتبر", show_alert=True)
        return

    user = query.from_user
    await query.answer("در حال ساخت لینک پرداخت...")

    log.info(
        "buy clicked telegram_id=%s plan=%s (%s)",
        user.id,
        plan.id,
        plan.title,
    )

    order_id = create_order(user.id, plan.id, plan.price_rial)

    try:
        authority, pay_url = await create_payment_link(
            plan.price_rial,
            f"VPN {plan.title} — سفارش {order_id}",
            order_id,
        )
        set_authority(order_id, authority)
        log.info(
            "payment link sent order=%s telegram_id=%s authority=%s",
            order_id,
            user.id,
            authority,
        )
    except Exception:
        log.exception("payment link failed order=%s", order_id)
        await query.edit_message_text("خطا در ساخت لینک پرداخت. دوباره /start بزن.")
        return

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("پرداخت", url=pay_url)]])
    await query.edit_message_text(
        f"پلن: {plan.title}\n"
        f"مبلغ: {plan.price_rial:,} ریال\n\n"
        "روی دکمه پرداخت بزن. بعد از تایید، پیام اشتراک برات میاد.",
        reply_markup=keyboard,
    )


async def on_unhandled_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    log.warning("unhandled callback data=%r", query.data)
    await query.answer("دکمه منقضی شده — /start بزن", show_alert=True)


def build_telegram_app() -> Application:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_error_handler(_on_error)
    app.add_handler(TypeHandler(Update, _log_update, block=False), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_buy, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_unhandled_callback))
    log.info("telegram handlers registered: start, buy callback, fallback callback")
    return app
