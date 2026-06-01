import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from bot.config import PLANS, SERVERS, TELEGRAM_BOT_TOKEN
from bot.config import Plan, Server
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


def _servers_keyboard(plan_index: int) -> InlineKeyboardMarkup:
    rows = []
    for i, s in enumerate(SERVERS):
        rows.append(
            [
                InlineKeyboardButton(
                    s.title,
                    callback_data=f"srv:{plan_index}:{i}",
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


async def _send_payment_link(
    query,
    plan: Plan,
    server: Server,
) -> None:
    user = query.from_user
    order_id = create_order(user.id, plan.id, server.id, plan.price_rial)

    try:
        authority, pay_url = await create_payment_link(
            plan.price_rial,
            f"VPN {plan.title} — {server.title} — سفارش {order_id}",
            order_id,
        )
        set_authority(order_id, authority)
        log.info(
            "payment link sent order=%s telegram_id=%s server=%s authority=%s",
            order_id,
            user.id,
            server.id,
            authority,
        )
    except Exception:
        log.exception("payment link failed order=%s", order_id)
        await query.edit_message_text("خطا در ساخت لینک پرداخت. دوباره /start بزن.")
        return

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("پرداخت", url=pay_url)]])
    await query.edit_message_text(
        f"پلن: {plan.title}\n"
        f"سرور: {server.title}\n"
        f"مبلغ: {plan.price_rial:,} ریال\n\n"
        "روی دکمه پرداخت بزن. بعد از تایید، پیام اشتراک برات میاد.",
        reply_markup=keyboard,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log.info("/start from telegram_id=%s username=%s", user.id, user.username)
    if not PLANS:
        await update.message.reply_text("هیچ پلنی تعریف نشده.")
        return
    if not SERVERS:
        await update.message.reply_text("هیچ سروری تعریف نشده.")
        return
    await update.message.reply_text(
        "سلام! یک پلن انتخاب کن.\n"
        "بعد از پرداخت موفق، لینک اشتراک VPN همین‌جا ارسال می‌شه.",
        reply_markup=_plans_keyboard(),
    )
    log.info("sent %d plan buttons", len(PLANS))


async def on_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    log.info("on_plan handler data=%r", query.data)

    try:
        plan_index = int(query.data.split(":", 1)[1])
        plan = PLANS[plan_index]
    except (ValueError, IndexError):
        log.warning("invalid plan callback data=%r", query.data)
        await query.answer("پلن نامعتبر", show_alert=True)
        return

    if len(SERVERS) == 1:
        await query.answer("در حال ساخت لینک پرداخت...")
        log.info(
            "plan selected telegram_id=%s plan=%s single_server=%s",
            query.from_user.id,
            plan.id,
            SERVERS[0].id,
        )
        await _send_payment_link(query, plan, SERVERS[0])
        return

    await query.answer()
    log.info(
        "plan selected telegram_id=%s plan=%s — showing %d servers",
        query.from_user.id,
        plan.id,
        len(SERVERS),
    )
    await query.edit_message_text(
        f"پلن: {plan.title}\n\nسرور را انتخاب کن:",
        reply_markup=_servers_keyboard(plan_index),
    )


async def on_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    log.info("on_server handler data=%r", query.data)

    try:
        _, plan_index_s, server_index_s = query.data.split(":", 2)
        plan = PLANS[int(plan_index_s)]
        server = SERVERS[int(server_index_s)]
    except (ValueError, IndexError):
        log.warning("invalid server callback data=%r", query.data)
        await query.answer("انتخاب نامعتبر", show_alert=True)
        return

    await query.answer("در حال ساخت لینک پرداخت...")
    log.info(
        "server selected telegram_id=%s plan=%s server=%s",
        query.from_user.id,
        plan.id,
        server.id,
    )
    await _send_payment_link(query, plan, server)


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
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_server, pattern=r"^srv:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_unhandled_callback))
    log.info("telegram handlers registered: start, plan, server, fallback")
    return app
