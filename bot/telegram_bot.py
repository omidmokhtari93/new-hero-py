import html
import logging
import time
from datetime import datetime
from urllib.parse import quote

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot.config import (
    ADMIN_CHAT_ID,
    ADMIN_USERNAME,
    BACKUP_INTERVAL_HOURS,
    DB_PATH,
    PLANS,
    SERVERS,
    SERVERS_BY_ID,
    TELEGRAM_BOT_TOKEN,
    Plan,
    Server,
)
from bot.db import create_order, get_all_users, get_order, get_user_orders, mark_paid, mark_failed
from bot.hiddify import create_user, subscription_url

log = logging.getLogger(__name__)

# Simple in-memory rate limiting
# {user_id: last_action_timestamp}
_user_last_action = {}
RATE_LIMIT_SECONDS = 1


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["خرید سرویس جدید"], ["سرویس های من", "راهنمای اتصال"], ["ارتباط با پشتیبانی"]],
        resize_keyboard=True,
    )


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


async def _rate_limit_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    
    if user_id and user_id != ADMIN_CHAT_ID:
        now = time.time()
        if user_id in _user_last_action:
            last_action = _user_last_action[user_id]
            if now - last_action < RATE_LIMIT_SECONDS:
                log.warning("Rate limit hit for user %s", user_id)
                raise ApplicationHandlerStop()
        _user_last_action[user_id] = now


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


async def _request_admin_approval(
    query,
    plan: Plan,
    server: Server,
) -> None:
    user = query.from_user
    order_id = create_order(user.id, plan.id, server.id, plan.price_rial)

    log.info(
        "order created, requesting admin approval order=%s telegram_id=%s plan=%s server=%s",
        order_id,
        user.id,
        plan.id,
        server.id,
    )

    # Message to user
    order_details = (
        f"سلام پشتیبان عزیز، قصد پرداخت این سفارش را دارم:\n\n"
        f"📦 شماره سفارش: {order_id}\n"
        f"💎 پلن: {plan.title}\n"
        f"🌍 لوکیشن: {server.title}\n"
        f"💰 مبلغ: {plan.price_rial:,} ریال"
    )
    encoded_text = quote(order_details)
    support_url = f"https://t.me/{ADMIN_USERNAME[1:]}?text={encoded_text}"

    await query.edit_message_text(
        f"✅ سفارش شما با موفقیت ثبت شد.\n\n"
        f"📦 شماره سفارش: {order_id}\n"
        f"💎 پلن: {plan.title}\n"
        f"🌍 لوکیشن: {server.title}\n"
        f"💰 مبلغ قابل پرداخت: {plan.price_rial:,} ریال\n\n"
        f"⚠️ برای فعال‌سازی سرویس، لطفاً روی دکمه زیر بزنید تا جزئیات سفارش برای پشتیبانی ارسال شود، سپس رسید واریز را در همان‌جا بفرستید.\n\n"
        f"🆔 پشتیبانی: {ADMIN_USERNAME}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📤 ارسال به پشتیبانی", url=support_url)]]
        ),
    )

    # Notification to admin
    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تایید", callback_data=f"adm_app:{order_id}"),
                InlineKeyboardButton("❌ لغو", callback_data=f"adm_can:{order_id}"),
            ]
        ]
    )
    
    admin_text = (
        f"🔔 <b>سفارش جدید دریافت شد!</b>\n\n"
        f"👤 کاربر: {html.escape(user.full_name)} (@{html.escape(user.username or '')})\n"
        f"🆔 آیدی عددی: <code>{user.id}</code>\n"
        f"📦 شماره سفارش: <code>{order_id}</code>\n"
        f"💎 پلن: {plan.title}\n"
        f"🌍 لوکیشن: {server.title}\n"
        f"💵 مبلغ: {plan.price_rial:,} ریال"
    )
    
    await query.get_bot().send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_text,
        reply_markup=admin_keyboard,
        parse_mode="HTML",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log.info("/start from telegram_id=%s username=%s", user.id, user.username)
    
    await update.message.reply_text(
        "سلام! به ربات فروش VPN خوش آمدید.\n"
        "یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=_main_keyboard(),
    )


async def buy_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PLANS:
        await update.message.reply_text("هیچ پلنی تعریف نشده.")
        return
    if not SERVERS:
        await update.message.reply_text("هیچ سروری تعریف نشده.")
        return
    await update.message.reply_text(
        "لطفاً یک پلن انتخاب کنید:",
        reply_markup=_plans_keyboard(),
    )


async def support_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"برای ارتباط با پشتیبانی به آیدی زیر پیام دهید:\n\n{ADMIN_USERNAME}"
    )


async def connection_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    guide_text = (
        "📖 <b>راهنمای اتصال به سرویس‌ها</b>\n\n"
        "برای استفاده از سرویس‌های ما، ابتدا باید لینک اشتراک (Subscription URL) خود را از بخش «سرویس‌های من» کپی کنید.\n\n"
        "📱 <b>اندروید و iOS (نرم‌افزار V2Box):</b>\n"
        "۱. نرم‌افزار <b>V2Box</b> را از اپ‌استور یا گوگل‌پلی نصب کنید.\n"
        "۲. وارد برنامه شده و به بخش <b>Configs</b> بروید.\n"
        "۳. روی علامت <b>+</b> در بالای صفحه بزنید.\n"
        "۴. گزینه <b>Add Subscription</b> یا <b>Import v2ray link from clipboard</b> را انتخاب کنید.\n"
        "۵. لینک اشتراک خود را وارد کرده و یک نام دلخواه بگذارید.\n"
        "۶. در نهایت دکمه <b>Update Subscriptions</b> را بزنید و به سرور متصل شوید.\n\n"
        "💻 <b>ویندوز (نرم‌افزار v2rayN):</b>\n"
        "۱. نرم‌افزار <b>v2rayN</b> را دانلود و اجرا کنید.\n"
        "۲. به بخش <b>Subscription Group</b> بروید.\n"
        "۳. گزینه <b>Add</b> را بزنید و در قسمت URL، لینک اشتراک خود را وارد کنید.\n"
        "۴. روی دکمه <b>Update subscription</b> بزنید تا لیست سرورها ظاهر شود.\n"
        "۵. یک سرور را انتخاب کرده و دکمه <b>Enter</b> را بزنید تا متصل شوید.\n\n"
        "⚠️ در صورت بروز هرگونه مشکل، با پشتیبانی در ارتباط باشید."
    )
    await update.message.reply_text(guide_text, parse_mode="HTML")


async def my_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    orders = get_user_orders(user.id, limit=5)

    if not orders:
        await update.message.reply_text(
            "🔎 شما هنوز هیچ سرویس فعالی ندارید.\n"
            "برای خرید سرویس جدید از دکمه زیر استفاده کنید."
        )
        return

    text = "👤 <b>سرویس‌های اخیر شما:</b>\n\n"
    for i, order in enumerate(orders, 1):
        server = SERVERS_BY_ID.get(order["server_id"])
        if not server:
            continue
            
        sub_url = subscription_url(server, order["hiddify_uuid"])
        
        # Simple date formatting from SQLite ISO string
        date_str = order["created_at"].split(" ")[0] if " " in order["created_at"] else order["created_at"]
        
        text += (
            f"{i}. 🌍 لوکیشن: {server.title}\n"
            f"📅 تاریخ فعال‌سازی: <code>{date_str}</code>\n"
            f"🔗 لینک اشتراک (برای کپی لمس کنید):\n<code>{sub_url}</code>\n"
            f"--------------------------\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    original_text = update.message.text
    if "#broadcast" not in original_text:
        return

    # Remove the hashtag and clean up
    broadcast_text = original_text.replace("#broadcast", "").strip()
    if not broadcast_text:
        await update.message.reply_text("⚠️ متن پیام همگانی خالی است.")
        return

    users = get_all_users()
    count = 0
    failed = 0

    status_msg = await update.message.reply_text(f"⏳ در حال ارسال پیام به {len(users)} کاربر...")

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=broadcast_text,
                parse_mode="HTML"
            )
            count += 1
        except Exception as e:
            log.warning("Failed to send broadcast to %s: %s", user_id, e)
            failed += 1

    await status_msg.edit_text(
        f"✅ ارسال پیام همگانی به پایان رسید.\n\n"
        f"📊 آمار:\n"
        f"✔️ موفق: {count}\n"
        f"❌ ناموفق: {failed}"
    )


async def send_db_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Starting scheduled DB backup to admin...")
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        with open(DB_PATH, "rb") as db_file:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=db_file,
                filename=f"orders_backup_{timestamp}.db",
                caption=f"📦 بک‌آپ خودکار دیتابیس\n📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        log.info("DB backup sent to admin successfully.")
    except Exception as e:
        log.error("Failed to send DB backup: %s", e)


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

    await query.answer()
    log.info(
        "plan selected telegram_id=%s plan=%s — showing %d servers",
        query.from_user.id,
        plan.id,
        len(SERVERS),
    )
    await query.edit_message_text(
        f"💎 پلن انتخاب شده: {plan.title}\n"
        f"💰 مبلغ: {plan.price_rial:,} ریال\n\n"
        f"📍 لطفاً لوکیشن (کشور) مورد نظر خود را برای خرید VPN انتخاب کنید:",
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

    await query.answer("در حال ثبت سفارش...")
    log.info(
        "server selected telegram_id=%s plan=%s server=%s",
        query.from_user.id,
        plan.id,
        server.id,
    )
    await _request_admin_approval(query, plan, server)


async def on_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("شما ادمین نیستید!", show_alert=True)
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)

    if not order or order["status"] != "pending":
        await query.answer("سفارش یافت نشد یا قبلاً تعیین تکلیف شده.", show_alert=True)
        return

    await query.answer("در حال ایجاد سرویس...")
    await query.edit_message_text(
        f"{query.message.text_html}\n\n⏳ در حال ایجاد سرویس...",
        parse_mode="HTML"
    )

    try:
        plan = next(p for p in PLANS if p.id == order["plan_id"])
        server = next(s for s in SERVERS if s.id == order["server_id"])
        
        user_data = await create_user(server, order["telegram_id"], plan)
        hiddify_uuid = user_data["uuid"]
        mark_paid(order_id, hiddify_uuid)

        sub_url = subscription_url(server, hiddify_uuid)

        # Notify user
        await context.bot.send_message(
            chat_id=order["telegram_id"],
            text=f"✅ سفارش شما تایید شد!\n\n"
            f"🔗 لینک اشتراک شما:\n<code>{sub_url}</code>\n\n"
            f"از خرید شما متشکریم!",
            parse_mode="HTML",
        )

        await query.edit_message_text(
            f"{query.message.text_html}\n\n✅ تایید شد و سرویس ایجاد گشت.\nUUID: <code>{hiddify_uuid}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        log.exception("failed to approve order %s", order_id)
        await query.edit_message_text(
            f"{query.message.text_html}\n\n❌ خطا در ایجاد سرویس: {html.escape(str(e))}",
            parse_mode="HTML"
        )


async def on_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("شما ادمین نیستید!", show_alert=True)
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)

    if not order or order["status"] != "pending":
        await query.answer("سفارش یافت نشد یا قبلاً تعیین تکلیف شده.", show_alert=True)
        return

    mark_failed(order_id)
    await query.answer("سفارش لغو شد.")

    # Notify user
    await context.bot.send_message(
        chat_id=order["telegram_id"],
        text=f"❌ متاسفانه سفارش شماره {order_id} شما لغو شد.\nدر صورت نیاز با پشتیبانی در ارتباط باشید.",
    )

    await query.edit_message_text(
        f"{query.message.text_html}\n\n❌ لغو شد.",
        parse_mode="HTML"
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
    # Group -2: Rate Limiting (Blocking)
    app.add_handler(TypeHandler(Update, _rate_limit_middleware), group=-2)
    # Group -1: Logging (Non-blocking)
    app.add_handler(TypeHandler(Update, _log_update), group=-1)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Text("خرید سرویس جدید"), buy_service))
    app.add_handler(MessageHandler(filters.Text("سرویس های من"), my_services))
    app.add_handler(MessageHandler(filters.Text("راهنمای اتصال"), connection_guide))
    app.add_handler(MessageHandler(filters.Text("ارتباط با پشتیبانی"), support_contact))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.Regex(r"#broadcast"), broadcast_message))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_server, pattern=r"^srv:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_approve, pattern=r"^adm_app:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_cancel, pattern=r"^adm_can:\d+$"))
    app.add_handler(CallbackQueryHandler(on_unhandled_callback))
    
    # Schedule DB backup
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(
            send_db_backup, 
            interval=BACKUP_INTERVAL_HOURS * 3600, 
            first=10  # First backup after 10 seconds of startup
        )
        log.info("DB backup job scheduled every %d hours", BACKUP_INTERVAL_HOURS)
    else:
        log.warning("JobQueue not available, DB backup will not run!")

    log.info("telegram handlers registered: start, plan, server, fallback")
    return app
