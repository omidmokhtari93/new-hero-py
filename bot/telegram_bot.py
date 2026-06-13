import html
import logging
import shutil
import time
import asyncio
import httpx
from datetime import datetime

import jdatetime
import pytz
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
    BOT_NAME,
    DB_PATH,
    PLANS,
    SERVERS,
    SERVERS_BY_ID,
    TELEGRAM_BOT_TOKEN,
    Plan,
    Server,
)
from bot.db import (
    count_all_orders,
    create_order,
    get_all_orders_paginated,
    get_all_users,
    get_order,
    get_db_user,
    get_user_orders,
    count_user_orders,
    count_user_active_orders,
    mark_paid,
    mark_failed,
    search_order_by_uuid,
    search_users,
    update_order_plan,
    upsert_user,
)
from bot.hiddify import (
    check_server_status,
    create_user,
    delete_user,
    get_system_stats,
    get_user,
    subscription_url,
    update_user_plan,
    update_user_status,
)

log = logging.getLogger(__name__)

# Simple in-memory rate limiting
_user_last_action = {}
RATE_LIMIT_SECONDS = 1

# Track last backup time to prevent duplicates
_last_backup_time = 0

TEHRAN_TZ = pytz.timezone("Asia/Tehran")


def _to_jalali(dt: datetime) -> str:
    """Convert a datetime object to Jalali string."""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    tehran_dt = dt.astimezone(TEHRAN_TZ)
    return jdatetime.datetime.fromgregorian(datetime=tehran_dt).strftime("%Y/%m/%d %H:%M")


def _get_jalali_now() -> str:
    """Get current time in Jalali string."""
    return _to_jalali(datetime.now(pytz.utc))


def _format_size(num_bytes: int) -> str:
    """Format bytes to human readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def _create_progress_bar(used: float, total: float, square: bool = False) -> str:
    """Create a progress bar with emojis."""
    if total == 0:
        return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜" if square else "⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪"
    
    percentage = (used / total) * 100
    num_filled = int(percentage / 10)
    num_empty = 10 - num_filled
    
    if square:
        filled = "🟩" * num_filled
        empty = "⬜" * num_empty
    else:
        filled = "🟢" * num_filled
        empty = "⚪" * num_empty
    
    return f"{filled}{empty} ({percentage:.0f}%)"


def _main_keyboard(user_id: int = None) -> ReplyKeyboardMarkup:
    buttons = [["🛍️ خرید سرویس جدید"], ["👤 سرویس‌های من", "📖 راهنمای اتصال"], ["👤 اطلاعات اکانت"], ["‍👨‍💻 ارتباط با پشتیبانی"]]
    if user_id == ADMIN_CHAT_ID:
        buttons.append(["📊 لیست همه سفارشات", "📊 وضعیت سرورها"])
        buttons.append(["➕ ایجاد سفارش"])
    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True,
    )


def _plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(PLANS):
        rows.append(
            [
                InlineKeyboardButton(
                    f"💎 {p.title} — {p.price_rial:,} ریال",
                    callback_data=f"buy:{i}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _servers_keyboard(plan_index: int, server_statuses: dict[str, bool]) -> InlineKeyboardMarkup:
    rows = []
    for i, s in enumerate(SERVERS):
        is_active = server_statuses.get(s.id, False)
        status_text = "🟢 فعال" if is_active else "🔴 غیرفعال"
        
        # If inactive, we can still show it but maybe with a different callback or alert
        # The user requested: "اگه غیرفعال بود نتونه انتخابش کنه"
        callback_data = f"srv:{plan_index}:{i}" if is_active else "inactive_server"
        
        rows.append(
            [
                InlineKeyboardButton(
                    f"{s.title} ({status_text})",
                    callback_data=callback_data,
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
    user = update.effective_user
    if user:
        # Update user's last_active_at
        upsert_user(
            telegram_id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username
        )
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
        f"قصد پرداخت این سفارش را دارم:\n\n"
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
    
    # Save or update user data
    upsert_user(
        telegram_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username
    )
    
    welcome_text = (
        f"سلام {html.escape(user.first_name)} عزیز،\n"
        f"ما اینجاییم که شمارو به اینترنت بین الملل متصل کنیم، همین 🙂.\n\n"
        f"👇 <b>برای خرید میتونید به منوی پایین مراجعه کنید:</b>"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=_main_keyboard(user.id),
        parse_mode="HTML"
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
        "برای استفاده از سرویس‌های ما از نرم‌افزار رسمی <b>Hiddify</b> استفاده کنید.\n\n"
        "🔑 <b>مرحله ۱ — دریافت لینک اشتراک</b>\n"
        "۱. در ربات روی «👤 سرویس‌های من» بزنید.\n"
        "۲. <b>لینک اشتراک (Subscription URL)</b> سرویس خود را کپی کنید.\n\n"
        "📥 <b>مرحله ۲ — دانلود Hiddify</b>\n"
        "🤖 <b>اندروید:</b>\n"
        "• <a href=\"https://play.google.com/store/apps/details?id=app.hiddify.com\">Google Play</a>\n"
        "• <a href=\"https://github.com/hiddify/hiddify-app/releases/latest/download/Hiddify-Android-universal.apk\">دانلود مستقیم APK</a>\n\n"
        "🍎 <b>iOS:</b>\n"
        "• <a href=\"https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532\">App Store</a>\n\n"
        "💻 <b>ویندوز:</b>\n"
        "• <a href=\"https://github.com/hiddify/hiddify-app/releases/latest/download/Hiddify-Windows-Setup-x64.exe\">دانلود نصب‌کننده</a>\n"
        "• <a href=\"https://github.com/hiddify/hiddify-app/releases\">صفحه دانلود GitHub</a>\n\n"
        "📲 <b>مرحله ۳ — افزودن پروفایل</b>\n"
        "۱. برنامه Hiddify را باز کنید.\n"
        "۲. در صفحه <b>Home</b> روی دکمه <b>+</b> (بالا سمت چپ) بزنید.\n"
        "۳. یکی از این روش‌ها را انتخاب کنید:\n"
        "   • <b>Add from clipboard</b> — اگر لینک را کپی کرده‌اید\n"
        "   • <b>Add manually</b> — نام دلخواه بگذارید و لینک را در فیلد URL وارد کنید\n"
        "۴. روی <b>Save</b> بزنید.\n\n"
        "🔌 <b>مرحله ۴ — اتصال</b>\n"
        "۱. به صفحه <b>Home</b> برگردید.\n"
        "۲. دکمه اتصال وسط صفحه را بزنید تا سبز شود.\n"
        "۳. برای به‌روزرسانی سرورها، دکمه بروزرسانی پروفایل را بزنید.\n"
        "۴. در منوی <b>Proxies</b> می‌توانید سرور دلخواه را انتخاب کنید.\n\n"
        "💡 <b>نکته:</b> اگر لینک اشتراک را در مرورگر باز کنید، گزینه «Tap to Start» پروفایل را مستقیم در Hiddify اضافه می‌کند.\n\n"
        "⚠️ در صورت بروز مشکل، با پشتیبانی در ارتباط باشید."
    )
    await update.message.reply_text(
        guide_text, parse_mode="HTML", disable_web_page_preview=True
    )


async def my_services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    orders = get_user_orders(user.id, limit=5)

    if not orders:
        await update.message.reply_text(
            "🔎 شما هنوز هیچ سرویس فعالی ندارید.\n"
            "برای خرید سرویس جدید از دکمه زیر استفاده کنید."
        )
        return

    msg = await update.message.reply_text("⏳ در حال دریافت اطلاعات سرویس‌ها...")

    # Fetch all user data in parallel
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        valid_orders = []
        for order in orders:
            server = SERVERS_BY_ID.get(order["server_id"])
            if server:
                tasks.append(get_user(server, order["hiddify_uuid"], client=client))
                valid_orders.append((order, server))
        
        h_users = await asyncio.gather(*tasks)

    text = "👤 <b>سرویس‌های اخیر شما:</b>\n\n"
    for (order, server), h_user in zip(valid_orders, h_users):
        sub_url = subscription_url(server, order["hiddify_uuid"], label=f"HeroVPN - {server.title}")
        
        usage_text = ""
        if h_user:
            usage_gb = h_user.get("current_usage_GB", 0)
            limit_gb = h_user.get("usage_limit_GB", 0)
            
            # Calculate remaining days
            rem_days = h_user.get("remaining_days")
            if rem_days is None or not isinstance(rem_days, (int, float)):
                # Try to calculate manually from start_date and package_days
                start_date_str = h_user.get("start_date")
                package_days = h_user.get("package_days", 0)
                if start_date_str and package_days:
                    try:
                        start_date = datetime.fromisoformat(start_date_str)
                        today = datetime.now(pytz.utc).date()
                        days_passed = (today - start_date.date()).days
                        rem_days = max(package_days - days_passed, 0)
                    except Exception as e:
                        log.warning(f"Failed to calculate remaining days: {e}")
                        rem_days = package_days  # Fallback to package days
                else:
                    rem_days = package_days or "نامحدود"
            
            progress_bar = _create_progress_bar(usage_gb, limit_gb, square=True)
            
            usage_text = (
                f"📊 مصرف: <code>{usage_gb:.2f}</code> از <code>{limit_gb}</code> گیگ\n"
                f"   {progress_bar}\n"
                f"⏳ زمان باقی‌مانده: <code>{rem_days}</code> روز\n"
            )

        # Parse SQLite UTC timestamp
        try:
            dt = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
            jalali_date = _to_jalali(dt)
        except Exception:
            jalali_date = order["created_at"]
        
        text += (
            f"📦 شماره سفارش: <code>{order['id']}</code>\n"
            f"🌍 لوکیشن: {server.title}\n"
            f"📅 تاریخ فعال‌سازی: <code>{jalali_date}</code>\n"
            f"{usage_text}"
            f"🔗 لینک اشتراک (برای کپی لمس کنید):\n<code>{sub_url}</code>\n"
            f"--------------------------\n"
        )

    await msg.edit_text(text, parse_mode="HTML")


async def account_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db_user = get_db_user(user.id)
    
    if not db_user:
        await update.message.reply_text(
            "⚠️ اطلاعات شما در دیتابیس یافت نشد. لطفاً دوباره /start بزنید."
        )
        return
    
    # Calculate join date
    try:
        dt = datetime.strptime(db_user["created_at"], "%Y-%m-%d %H:%M:%S")
        jalali_join_date = _to_jalali(dt)
    except Exception:
        jalali_join_date = db_user["created_at"]
    
    # Get order statistics
    total_orders = count_user_orders(user.id)
    active_orders = count_user_active_orders(user.id)
    
    # Build user info
    full_name = " ".join([db_user["first_name"], db_user["last_name"] if db_user["last_name"] else ""])
    username = f"@{db_user['username']}" if db_user["username"] else "—"
    
    text = (
        f"👤 <b>اطلاعات اکانت شما:</b>\n\n"
        f"🆔 <b>شماره چت (Chat ID):</b>\n"
        f"<code>{user.id}</code>\n\n"
        f"👨‍🦱 <b>نام و نام خانوادگی:</b>\n"
        f"{full_name}\n\n"
        f"🔖 <b>نام کاربری:</b>\n"
        f"{username}\n\n"
        f"📅 <b>تاریخ عضویت:</b>\n"
        f"{jalali_join_date}\n\n"
        f"📦 <b>تعداد کل سفارشات:</b>\n"
        f"{total_orders}\n\n"
        f"✅ <b>تعداد سرویس‌های فعال:</b>\n"
        f"{active_orders}"
    )
    
    await update.message.reply_text(text, parse_mode="HTML")


async def admin_create_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    
    await update.message.reply_text(
        "👤 لطفاً نام، نام کاربری یا آیدی عددی کاربر را وارد کنید:"
    )
    
    # Set the state for this admin
    context.user_data["admin_state"] = "create_order_search_user"


async def admin_search_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    
    state = context.user_data.get("admin_state")
    if state != "create_order_search_user":
        return
    
    search_query = update.message.text.strip()
    users = search_users(search_query)
    
    if not users:
        await update.message.reply_text("❌ کاربری با این مشخصات یافت نشد.")
        context.user_data.pop("admin_state", None)
        return
    
    # Show keyboard with users
    keyboard_rows = []
    for user in users:
        full_name = " ".join([user["first_name"], user["last_name"] if user["last_name"] else ""])
        username = f"@{user['username']}" if user["username"] else ""
        button_text = f"{full_name} {username}"
        keyboard_rows.append([InlineKeyboardButton(button_text, callback_data=f"adm_create_order_user:{user['telegram_id']}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard_rows)
    await update.message.reply_text("👥 لطفاً کاربر مورد نظر را انتخاب کنید:", reply_markup=reply_markup)
    context.user_data.pop("admin_state", None)


async def admin_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await _send_orders_page(update, 0)


async def _generate_stats_text() -> str:
    text = f"📊 <b>وضعیت لحظه‌ای سرورها</b>\n"
    text += f"📅 به‌روزرسانی: <code>{_get_jalali_now()}</code>\n\n"
    
    for s in SERVERS:
        data = await get_system_stats(s)
        if not data:
            text += f"📍 <b>{s.title}:</b>\n❌ عدم برقراری ارتباط با پنل\n\n"
            continue
            
        try:
            # Extract data based on the provided Hiddify JSON structure
            sys_stats = data.get("stats", {}).get("system", {})
            usage_hist = data.get("usage_history", {})
            
            total_users = usage_hist.get("total", {}).get("users", 0)
            online_last5min = usage_hist.get("m5", {}).get("online", 0)
            unique_ips = sys_stats.get("total_unique_ips", 0)
            
            cpu = sys_stats.get("cpu_percent", 0)
            ram_used = sys_stats.get("ram_used", 0)
            ram_total = sys_stats.get("ram_total", 1) # avoid div by zero
            ram_percent = (ram_used / ram_total) * 100
            
            # Network traffic (current)
            net_recv = sys_stats.get("bytes_recv", 0)
            net_sent = sys_stats.get("bytes_sent", 0)
            
            total_traffic_gb = sys_stats.get("net_total_cumulative_GB", 0)
            
            # Today usage (convert bytes to GB)
            today_usage_bytes = usage_hist.get("today", {}).get("usage", 0)
            if isinstance(today_usage_bytes, str):
                today_usage_bytes = int(today_usage_bytes)
            today_traffic_gb = today_usage_bytes / (1024**3)
            
            text += (
                f"📍 <b>{s.title}:</b>\n"
                f"👥 کل کاربران: <code>{total_users}</code>\n"
                f"🟢 آنلاین: <code>{online_last5min}</code>\n"
                f"💻 پردازنده: <code>{cpu}%</code> | رم: <code>{ram_percent:.1f}%</code>\n"
                f"📡 ترافیک زنده شبکه:\n"
                f"   📥 ورودی: <code>{_format_size(net_recv)}/s</code>\n"
                f"   📤 خروجی: <code>{_format_size(net_sent)}/s</code>\n"
                f"📅 مصرف امروز: <code>{today_traffic_gb:.2f} GB</code>\n"
                f"📊 کل ترافیک (Net): <code>{total_traffic_gb:.2f} GB</code>\n"
                f"--------------------------\n"
            )
        except Exception as e:
            log.error("Error parsing stats for server %s: %s", s.id, e)
            text += f"📍 <b>{s.title}:</b>\n⚠️ خطا در پردازش داده‌ها\n\n"
            
    return text


async def admin_server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    
    msg = await update.message.reply_text("⏳ در حال دریافت آمار از سرورها...")
    text = await _generate_stats_text()
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 آپدیت", callback_data="adm_stats_ref")]])
    await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


async def on_admin_stats_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return
    
    await query.answer("در حال به‌روزرسانی آمار...")
    text = await _generate_stats_text()
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 آپدیت", callback_data="adm_stats_ref")]])
    # Only edit if text changed or to show it's refreshed
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        if "Message is not modified" in str(e):
            await query.answer("آمار تغییری نکرده است.")
        else:
            log.error("Error refreshing stats: %s", e)


async def _send_orders_page(update: Update, page: int) -> None:
    limit = 5
    offset = page * limit
    orders = get_all_orders_paginated(limit, offset)
    total_orders = count_all_orders()
    total_pages = (total_orders + limit - 1) // limit

    if not orders:
        text = "📭 هیچ سفارشی یافت نشد."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    # Fetch usage data for 'paid' orders in parallel
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = []
        valid_orders_info = []
        for order in orders:
            server = SERVERS_BY_ID.get(order["server_id"])
            if order["status"] == "paid" and server:
                tasks.append(get_user(server, order["hiddify_uuid"], client=client))
                valid_orders_info.append((order, server))
            else:
                valid_orders_info.append((order, None))
        
        h_users_results = await asyncio.gather(*tasks)
        
        # Map results back to orders
        h_users_map = {}
        res_idx = 0
        for order, server in valid_orders_info:
            if order["status"] == "paid" and server:
                h_users_map[order["id"]] = h_users_results[res_idx]
                res_idx += 1

    text = f"📊 <b>لیست تمامی سفارشات (صفحه {page + 1} از {total_pages}):</b>\n\n"
    for order in orders:
        server = SERVERS_BY_ID.get(order["server_id"])
        status_icon = "✅" if order["status"] == "paid" else "⏳" if order["status"] == "pending" else "❌"
        
        usage_info = ""
        if order["status"] == "paid" and server:
            h_user = h_users_map.get(order["id"])
            if h_user:
                usage_gb = h_user.get("current_usage_GB", 0)
                limit_gb = h_user.get("usage_limit_GB", 0)
                
                # Calculate remaining days
                rem_days = h_user.get("remaining_days")
                if rem_days is None or not isinstance(rem_days, (int, float)):
                    # Try to calculate manually from start_date and package_days
                    start_date_str = h_user.get("start_date")
                    package_days = h_user.get("package_days", 0)
                    if start_date_str and package_days:
                        try:
                            start_date = datetime.fromisoformat(start_date_str)
                            today = datetime.now(pytz.utc).date()
                            days_passed = (today - start_date.date()).days
                            rem_days = max(package_days - days_passed, 0)
                        except Exception as e:
                            log.warning(f"Failed to calculate remaining days: {e}")
                            rem_days = package_days  # Fallback to package days
                    else:
                        rem_days = package_days or "نامحدود"
                
                progress_bar = _create_progress_bar(usage_gb, limit_gb, square=True)
                
                usage_info = (
                    f"📊 مصرف: <code>{usage_gb:.2f}/{limit_gb}</code> گیگ\n"
                    f"   {progress_bar}\n"
                    f"⏳ زمان باقی‌مانده: <code>{rem_days}</code> روز\n"
                )

        try:
            dt = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
            jalali_date = _to_jalali(dt)
        except Exception:
            jalali_date = order["created_at"]

        text += (
            f"{status_icon} سفارش <code>{order['id']}</code> | 👤 {order['telegram_id']}\n"
            f"📅 <code>{jalali_date}</code> | 🌍 {server.title if server else 'نامشخص'}\n"
            f"{usage_info}"
            f"--------------------------\n"
        )

    # Pagination buttons
    keyboard_rows = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"adm_orders:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"adm_orders:{page + 1}"))
    
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def on_admin_orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return
    
    await query.answer()
    page = int(query.data.split(":")[1])
    await _send_orders_page(update, page)


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
                parse_mode="HTML",
                reply_markup=_main_keyboard(user_id)
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


async def update_users_from_json(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not update.message.document:
        await update.message.reply_text("⚠️ لطفاً فایل JSON حاوی chat_id ها را ارسال کنید.")
        return

    status_msg = await update.message.reply_text("⏳ در حال پردازش فایل و به‌روزرسانی کاربران...")

    try:
        # Download the file
        file = await context.bot.get_file(update.message.document.file_id)
        file_content = await file.download_as_bytearray()

        # Parse JSON
        import json
        chat_ids = json.loads(file_content.decode('utf-8'))

        if not isinstance(chat_ids, list):
            await status_msg.edit_text("❌ فرمت فایل صحیح نیست. لطفاً یک آرایه از chat_id ها ارسال کنید.")
            return

        total = len(chat_ids)
        success_count = 0
        failed_count = 0

        log.info(f"Starting to update {total} users from JSON file")

        for index, chat_id in enumerate(chat_ids):
            try:
                # Fetch user info from Telegram
                user = await context.bot.get_chat(chat_id)

                # Save user to database
                upsert_user(
                    telegram_id=user.id,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    username=user.username
                )

                success_count += 1
                log.debug(f"Processed user {index+1}/{total}: {chat_id}")

                # Update status every 10 users
                if (index + 1) % 10 == 0:
                    await status_msg.edit_text(
                        f"⏳ در حال پردازش...\n"
                        f"📊 پیشرفت: {index+1}/{total}\n"
                        f"✔️ موفق: {success_count}\n"
                        f"❌ ناموفق: {failed_count}"
                    )

            except Exception as e:
                failed_count += 1
                log.error(f"Failed to process user {chat_id}: {e}")

        # Final status update
        await status_msg.edit_text(
            f"✅ به‌روزرسانی کاربران به پایان رسید.\n\n"
            f"📊 آمار نهایی:\n"
            f"👥 کل: {total}\n"
            f"✔️ موفق: {success_count}\n"
            f"❌ ناموفق: {failed_count}"
        )
        log.info(f"User update completed: total={total}, success={success_count}, failed={failed_count}")

    except Exception as e:
        log.exception(f"Failed to process update users: {e}")
        await status_msg.edit_text(f"❌ خطا در پردازش فایل: {e}")


async def _refresh_search_message(query, order_id: int) -> None:
    order = get_order(order_id)
    if not order:
        await query.answer("سفارش یافت نشد.", show_alert=True)
        return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server:
        await query.answer("سرور یافت نشد.", show_alert=True)
        return

    # Fetch fresh data from Hiddify
    h_user = await get_user(server, order["hiddify_uuid"])
    
    usage_info = ""
    status_text = "❓ نامشخص"
    if h_user:
        usage_gb = h_user.get("current_usage_GB", 0)
        limit_gb = h_user.get("usage_limit_GB", 0)
        
        # Calculate remaining days
        rem_days = h_user.get("remaining_days")
        if rem_days is None or not isinstance(rem_days, (int, float)):
            # Try to calculate manually from start_date and package_days
            start_date_str = h_user.get("start_date")
            package_days = h_user.get("package_days", 0)
            if start_date_str and package_days:
                try:
                    start_date = datetime.fromisoformat(start_date_str)
                    today = datetime.now(pytz.utc).date()
                    days_passed = (today - start_date.date()).days
                    rem_days = max(package_days - days_passed, 0)
                except Exception as e:
                    log.warning(f"Failed to calculate remaining days: {e}")
                    rem_days = package_days  # Fallback to package days
            else:
                rem_days = package_days or "نامحدود"

        is_enabled = h_user.get("enable", True)
        
        status_text = "✅ فعال" if is_enabled else "🔒 غیرفعال"
        usage_info = (
            f"📊 مصرف: <code>{usage_gb:.2f}</code> از <code>{limit_gb}</code> گیگ\n"
            f"⏳ زمان باقی‌مانده: <code>{rem_days}</code> روز\n"
            f"🛡️ وضعیت: <b>{status_text}</b>\n"
        )

    sub_url = subscription_url(server, order["hiddify_uuid"], label=f"HeroVPN - {server.title}")
    
    try:
        dt = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
        jalali_date = _to_jalali(dt)
    except Exception:
        jalali_date = order["created_at"]

    text = (
        f"🔍 <b>اطلاعات سرویس به‌روز شده:</b>\n\n"
        f"📦 شماره سفارش: <code>{order['id']}</code>\n"
        f"🆔 آیدی کاربر: <code>{order['telegram_id']}</code>\n"
        f"💎 پلن: {order['plan_id']}\n"
        f"🌍 لوکیشن: {server.title}\n"
        f"📅 تاریخ فعال‌سازی: <code>{jalali_date}</code>\n"
        f"{usage_info}"
        f"💵 مبلغ پرداخت شده: {order['amount_rial']:,} ریال\n"
        f"🔑 UUID: <code>{order['hiddify_uuid']}</code>\n\n"
        f"🔗 لینک اشتراک:\n<code>{sub_url}</code>"
    )

    admin_actions = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔓 فعال‌سازی", callback_data=f"adm_ena:{order['id']}"),
                InlineKeyboardButton("🔒 غیرفعال‌سازی", callback_data=f"adm_dis:{order['id']}"),
            ],
            [
                InlineKeyboardButton("🗑️ حذف کامل سرویس", callback_data=f"adm_del:{order['id']}"),
            ],
            [
                InlineKeyboardButton("🔄 تمدید سرویس", callback_data=f"adm_renew_menu:{order['id']}"),
            ],
        ]
    )

    await query.edit_message_text(text, reply_markup=admin_actions, parse_mode="HTML")


async def search_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    original_text = update.message.text
    # Extract search query (UUID or URL)
    search_query = original_text.replace("#search", "").strip()
    
    if not search_query:
        await update.message.reply_text("⚠️ لطفا شماره سفارش، UUID یا لینک ساب را برای جستجو وارد کنید.")
        return

    order = None
    # Try searching by Order ID first if query is numeric
    if search_query.isdigit():
        order = get_order(int(search_query))

    # If not found by ID, try UUID/URL
    if not order:
        uuid = search_query
        if "/" in search_query:
            uuid = search_query.rstrip("/").split("/")[-1]
        order = search_order_by_uuid(uuid)
    
    if not order:
        await update.message.reply_text("❌ سفارشی با این مشخصات یافت نشد.")
        return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server:
        await update.message.reply_text("❌ سرور مربوط به این سفارش دیگر وجود ندارد.")
        return

    sub_url = subscription_url(server, order["hiddify_uuid"], label=f"HeroVPN - {server.title}")
    
    # Get usage info from Hiddify for admin search
    h_user = await get_user(server, order["hiddify_uuid"])
    usage_info = ""
    if h_user:
        usage_gb = h_user.get("current_usage_GB", 0)
        limit_gb = h_user.get("usage_limit_GB", 0)
        
        # Robust extraction of remaining days
        rem_days = h_user.get("remaining_days")
        if rem_days is None:
            # Fallback to package_days if countdown hasn't started
            rem_days = h_user.get("package_days", "نامحدود")

        usage_info = (
            f"📊 مصرف: <code>{usage_gb:.2f}</code> از <code>{limit_gb}</code> گیگ\n"
            f"⏳ زمان باقی‌مانده: <code>{rem_days}</code> روز\n"
        )

    # Parse date
    try:
        dt = datetime.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S")
        jalali_date = _to_jalali(dt)
    except Exception:
        jalali_date = order["created_at"]

    user_info = f"👤 کاربر: {order['telegram_id']}"
    
    # Get plan title
    plan_title = order['plan_id']
    for plan in PLANS:
        if plan.id == order['plan_id']:
            plan_title = plan.title
            break
    
    text = (
        f"🔍 <b>اطلاعات سرویس یافت شده:</b>\n\n"
        f"📦 شماره سفارش: <code>{order['id']}</code>\n"
        f"🆔 آیدی کاربر: <code>{order['telegram_id']}</code>\n"
        f"💎 پلن: {plan_title}\n"
        f"🌍 لوکیشن: {server.title}\n"
        f"📅 تاریخ فعال‌سازی: <code>{jalali_date}</code>\n"
        f"{usage_info}"
        f"💵 مبلغ پرداخت شده: {order['amount_rial']:,} ریال\n"
        f"🔑 UUID: <code>{order['hiddify_uuid']}</code>\n\n"
        f"🔗 لینک اشتراک:\n<code>{sub_url}</code>"
    )

    admin_actions = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔓 فعال‌سازی", callback_data=f"adm_ena:{order['id']}"),
                InlineKeyboardButton("🔒 غیرفعال‌سازی", callback_data=f"adm_dis:{order['id']}"),
            ],
            [
                InlineKeyboardButton("🗑️ حذف کامل سرویس", callback_data=f"adm_del:{order['id']}"),
            ],
            [
                InlineKeyboardButton("🔄 تمدید سرویس", callback_data=f"adm_renew_menu:{order['id']}"),
            ],
        ]
    )

    await update.message.reply_text(text, reply_markup=admin_actions, parse_mode="HTML")


async def send_db_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_backup_time
    now = time.time()
    
    # Don't send if last backup was less than 50 minutes ago
    if now - _last_backup_time < 3000:
        log.info("Skipping scheduled backup, last one was too recent.")
        return

    log.info("Starting scheduled DB backup to admin...")
    try:
        now_dt = datetime.now(pytz.utc)
        timestamp = now_dt.strftime("%Y-%m-%d_%H-%M")
        jalali_now = _to_jalali(now_dt)
        
        with open(DB_PATH, "rb") as db_file:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=db_file,
                filename=f"orders_backup_{timestamp}.db",
                caption=f"📦 بک‌آپ خودکار دیتابیس\n📅 تاریخ: <code>{jalali_now}</code>",
                parse_mode="HTML"
            )
        _last_backup_time = now
        log.info("DB backup sent to admin successfully.")
    except Exception as e:
        log.error("Failed to send DB backup: %s", e)


async def restore_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_CHAT_ID:
        return

    if not update.message.document or not update.message.document.file_name.endswith(".db"):
        await update.message.reply_text("⚠️ لطفا یک فایل با پسوند .db ارسال کنید.")
        return

    try:
        status_msg = await update.message.reply_text("⏳ در حال بازیابی دیتابیس...")
        
        # Download the file
        new_file = await context.bot.get_file(update.message.document.file_id)
        
        # Create a backup of current DB before overwriting
        backup_path = f"{DB_PATH}.bak"
        shutil.copy2(DB_PATH, backup_path)
        
        # Save the new file
        await new_file.download_to_drive(DB_PATH)
        
        await status_msg.edit_text(
            "✅ دیتابیس با موفقیت بازیابی شد.\n"
            f"نسخه قبلی جهت اطمینان در فایل <code>{backup_path}</code> ذخیره گردید.",
            parse_mode="HTML"
        )
        log.info("Database restored by admin from telegram file.")
    except Exception as e:
        log.error("Database restore failed: %s", e)
        await update.message.reply_text(f"❌ خطا در بازیابی دیتابیس: {e}")


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
    
    # Show loading message
    await query.edit_message_text(
        f"💎 پلن انتخاب شده: {plan.title}\n"
        "⏳ در حال بررسی وضعیت سرورها... لطفاً کمی صبر کنید."
    )

    # Check server statuses in parallel
    async with httpx.AsyncClient(timeout=3) as client:
        tasks = [check_server_status(s, client=client) for s in SERVERS]
        results = await asyncio.gather(*tasks)
        server_statuses = {s.id: res for s, res in zip(SERVERS, results)}

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
        reply_markup=_servers_keyboard(plan_index, server_statuses),
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

        sub_url = subscription_url(server, hiddify_uuid, label=f"HeroVPN - {server.title}")

        # Notify user
        await context.bot.send_message(
            chat_id=order["telegram_id"],
            text=f"✅ سفارش شما تایید شد!\n\n"
            f"🔗 لینک اشتراک شما:\n<code>{sub_url}</code>\n\n"
            f"از خرید شما متشکریم!",
            parse_mode="HTML",
        )

        await query.edit_message_text(
            f"{query.message.text_html}\n\n✅ تایید شد و سرویس ایجاد گشت.\nUUID: <code>{hiddify_uuid}</code>\n🔗 لینک اشتراک:\n<code>{sub_url}</code>",
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


async def on_admin_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)
    if not order: return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server: return

    await query.answer("در حال فعال‌سازی...")
    ok = await update_user_status(server, order["hiddify_uuid"], enable=True)
    
    if ok:
        await _refresh_search_message(query, order_id)
        await query.answer("✅ سرویس فعال شد", show_alert=True)
    else:
        await query.answer("❌ خطا در فعال‌سازی", show_alert=True)


async def on_admin_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)
    if not order: return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server: return

    await query.answer("در حال غیرفعال‌سازی...")
    ok = await update_user_status(server, order["hiddify_uuid"], enable=False)
    
    if ok:
        await _refresh_search_message(query, order_id)
        await query.answer("🔒 سرویس غیرفعال شد", show_alert=True)
    else:
        await query.answer("❌ خطا در غیرفعال‌سازی", show_alert=True)


async def on_admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)
    if not order: return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server: return

    await query.answer("در حال حذف...")
    ok = await delete_user(server, order["hiddify_uuid"])
    
    if ok:
        mark_failed(order_id) # Mark as failed/deleted in DB
        await query.edit_message_text(f"🗑️ <b>سرویس با موفقیت از پنل هیدیفای و دیتابیس ربات حذف شد.</b>\n\n📦 شماره سفارش: <code>{order_id}</code>", parse_mode="HTML")
        await query.answer("✅ حذف شد", show_alert=True)
    else:
        await query.answer("❌ خطا در حذف از پنل", show_alert=True)


async def on_admin_renew_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    order_id = int(query.data.split(":")[1])
    order = get_order(order_id)
    if not order:
        await query.answer("سفارش یافت نشد.", show_alert=True)
        return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server:
        await query.answer("سرور یافت نشد.", show_alert=True)
        return

    # Check if server is active
    server_is_active = await check_server_status(server)
    if not server_is_active:
        await query.answer("⚠️ سرور مربوطه غیرفعال است.", show_alert=True)
        return

    # Create keyboard with plan options
    keyboard_rows = []
    for i, plan in enumerate(PLANS):
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"💎 {plan.title} - {plan.price_rial:,} ریال",
                    callback_data=f"adm_renew:{order_id}:{i}"
                )
            ]
        )
    # Add back button
    keyboard_rows.append(
        [InlineKeyboardButton("⬅️ بازگشت", callback_data=f"adm_back:{order_id}")]
    )

    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    text = (
        f"🔄 <b>تمدید سرویس</b>\n\n"
        f"📦 شماره سفارش: <code>{order_id}</code>\n"
        f"🌍 لوکیشن: {server.title}\n\n"
        f"لطفاً پلن جدید را انتخاب کنید:"
    )

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def on_admin_renew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        _, order_id_s, plan_index_s = query.data.split(":", 2)
        order_id = int(order_id_s)
        plan_index = int(plan_index_s)
    except (ValueError, IndexError):
        await query.answer("انتخاب نامعتبر.", show_alert=True)
        return

    order = get_order(order_id)
    if not order:
        await query.answer("سفارش یافت نشد.", show_alert=True)
        return

    server = SERVERS_BY_ID.get(order["server_id"])
    if not server:
        await query.answer("سرور یافت نشد.", show_alert=True)
        return

    plan = PLANS[plan_index]
    if not plan:
        await query.answer("پلن یافت نشد.", show_alert=True)
        return

    await query.answer("در حال تمدید...")

    # Update the user's plan on Hiddify
    ok = await update_user_plan(server, order["hiddify_uuid"], plan)

    if ok:
        # Update the order record in the database
        update_order_plan(order_id, plan.id, plan.price_rial)
        
        # Notify the user
        sub_url = subscription_url(server, order["hiddify_uuid"], label=f"HeroVPN - {server.title}")
        try:
            await context.bot.send_message(
                chat_id=order["telegram_id"],
                text=f"✅ سرویس شما با موفقیت تمدید شد!\n\n"
                     f"💎 پلن جدید: {plan.title}\n"
                     f"🔗 لینک اشتراک:\n<code>{sub_url}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            log.error("Failed to send renewal notification to user %s: %s", order["telegram_id"], e)
        
        # Refresh and show the updated service info
        await _refresh_search_message(query, order_id)
        await query.answer("✅ سرویس با موفقیت تمدید شد.", show_alert=True)
    else:
        await query.answer("❌ خطا در تمدید سرویس.", show_alert=True)


async def on_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    order_id = int(query.data.split(":")[1])
    await _refresh_search_message(query, order_id)


async def on_admin_create_order_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        telegram_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        await query.answer("انتخاب نامعتبر.", show_alert=True)
        return

    user = get_db_user(telegram_id)
    if not user:
        await query.answer("کاربر یافت نشد.", show_alert=True)
        return

    # Store the user in context
    context.user_data["create_order_user"] = telegram_id

    # Show plan selection
    keyboard_rows = []
    for i, plan in enumerate(PLANS):
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"💎 {plan.title} - {plan.price_rial:,} ریال",
                    callback_data=f"adm_create_order_plan:{telegram_id}:{i}"
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard_rows)
    await query.edit_message_text(
        "💎 لطفاً پلن مورد نظر را انتخاب کنید:",
        reply_markup=reply_markup
    )


async def on_admin_create_order_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        _, telegram_id_s, plan_index_s = query.data.split(":", 2)
        telegram_id = int(telegram_id_s)
        plan_index = int(plan_index_s)
    except (ValueError, IndexError):
        await query.answer("انتخاب نامعتبر.", show_alert=True)
        return

    plan = PLANS[plan_index]
    if not plan:
        await query.answer("پلن یافت نشد.", show_alert=True)
        return

    # Store plan in context
    context.user_data["create_order_plan"] = plan_index

    # Show loading message
    await query.edit_message_text(
        "⏳ در حال بررسی وضعیت سرورها..."
    )

    # Check server statuses in parallel
    async with httpx.AsyncClient(timeout=3) as client:
        tasks = [check_server_status(s, client=client) for s in SERVERS]
        results = await asyncio.gather(*tasks)
        server_statuses = {s.id: res for s, res in zip(SERVERS, results)}

    # Show server selection
    keyboard_rows = []
    for i, server in enumerate(SERVERS):
        is_active = server_statuses.get(server.id, False)
        status_text = "🟢 فعال" if is_active else "🔴 غیرفعال"
        if is_active:
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        f"🌍 {server.title} ({status_text})",
                        callback_data=f"adm_create_order_server:{telegram_id}:{plan_index}:{i}"
                    )
                ]
            )

    reply_markup = InlineKeyboardMarkup(keyboard_rows)
    await query.edit_message_text(
        "🌍 لطفاً سرور مورد نظر را انتخاب کنید:",
        reply_markup=reply_markup
    )


async def on_admin_create_order_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_CHAT_ID:
        return

    try:
        _, telegram_id_s, plan_index_s, server_index_s = query.data.split(":", 3)
        telegram_id = int(telegram_id_s)
        plan_index = int(plan_index_s)
        server_index = int(server_index_s)
    except (ValueError, IndexError):
        await query.answer("انتخاب نامعتبر.", show_alert=True)
        return

    plan = PLANS[plan_index]
    server = SERVERS[server_index]

    await query.answer("در حال ایجاد سرویس...")
    await query.edit_message_text("⏳ در حال ایجاد سرویس...")

    try:
        # Create user on Hiddify
        user_data = await create_user(server, telegram_id, plan)
        hiddify_uuid = user_data["uuid"]

        # Create order in DB
        order_id = create_order(telegram_id, plan.id, server.id, plan.price_rial)
        mark_paid(order_id, hiddify_uuid)

        # Generate subscription link
        sub_url = subscription_url(server, hiddify_uuid, label=f"HeroVPN - {server.title}")

        # Notify user
        await context.bot.send_message(
            chat_id=telegram_id,
            text=f"✅ سرویس جدید برای شما فعال شد!\n\n"
                 f"💎 پلن: {plan.title}\n"
                 f"🌍 لوکیشن: {server.title}\n"
                 f"🔗 لینک اشتراک:\n<code>{sub_url}</code>",
            parse_mode="HTML",
        )

        # Show success message to admin
        await query.edit_message_text(
            f"✅ سرویس با موفقیت ایجاد شد!\n\n"
            f"📦 شماره سفارش: <code>{order_id}</code>\n"
            f"👤 آیدی کاربر: <code>{telegram_id}</code>\n"
            f"💎 پلن: {plan.title}\n"
            f"🌍 لوکیشن: {server.title}\n"
            f"🔗 لینک اشتراک:\n<code>{sub_url}</code>",
            parse_mode="HTML"
        )

        log.info(f"Admin created order {order_id} for user {telegram_id}")

    except Exception as e:
        log.error(f"Failed to create order for user {telegram_id}: {e}")
        await query.edit_message_text(f"❌ خطا در ایجاد سرویس: {e}")


async def on_inactive_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("⚠️ این سرور در حال حاضر غیرفعال است. لطفاً سرور دیگری را انتخاب کنید.", show_alert=True)


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
    app.add_handler(MessageHandler(filters.Text("🛍️ خرید سرویس جدید"), buy_service))
    app.add_handler(MessageHandler(filters.Text("👤 سرویس‌های من"), my_services))
    app.add_handler(MessageHandler(filters.Text("👤 اطلاعات اکانت"), account_info))
    app.add_handler(MessageHandler(filters.Text("📖 راهنمای اتصال"), connection_guide))
    app.add_handler(MessageHandler(filters.Text("👨‍💻 ارتباط با پشتیبانی"), support_contact))
    app.add_handler(MessageHandler(filters.Text("📊 لیست همه سفارشات"), admin_all_orders))
    app.add_handler(MessageHandler(filters.Text("📊 وضعیت سرورها"), admin_server_stats))
    app.add_handler(MessageHandler(filters.Text("➕ ایجاد سفارش"), admin_create_order))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.Regex(r"#broadcast"), broadcast_message))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.Regex(r"#search"), search_order))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.Document.ALL & filters.CaptionRegex(r"#restore"), restore_db))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.Document.ALL & filters.CaptionRegex(r"#update_users"), update_users_from_json))
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID), admin_search_user_handler))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(on_inactive_server, pattern=r"^inactive_server$"))
    app.add_handler(CallbackQueryHandler(on_server, pattern=r"^srv:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_approve, pattern=r"^adm_app:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_cancel, pattern=r"^adm_can:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_enable, pattern=r"^adm_ena:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_disable, pattern=r"^adm_dis:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_delete, pattern=r"^adm_del:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_renew_menu, pattern=r"^adm_renew_menu:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_renew, pattern=r"^adm_renew:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_back, pattern=r"^adm_back:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_create_order_user, pattern=r"^adm_create_order_user:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_create_order_plan, pattern=r"^adm_create_order_plan:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_create_order_server, pattern=r"^adm_create_order_server:\d+:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_orders_page, pattern=r"^adm_orders:\d+$"))
    app.add_handler(CallbackQueryHandler(on_admin_stats_refresh, pattern=r"^adm_stats_ref$"))
    app.add_handler(CallbackQueryHandler(on_unhandled_callback))
    
    # Schedule DB backup
    job_queue = app.job_queue
    if job_queue:
        # Check if job already exists to prevent duplicates
        if not job_queue.get_jobs_by_name("db_backup"):
            job_queue.run_repeating(
                send_db_backup, 
                interval=BACKUP_INTERVAL_HOURS * 3600, 
                first=BACKUP_INTERVAL_HOURS * 3600,  # Start after the interval
                name="db_backup"
            )
            log.info("DB backup job scheduled every %d hours", BACKUP_INTERVAL_HOURS)
    else:
        log.warning("JobQueue not available, DB backup will not run!")

    log.info("telegram handlers registered: start, plan, server, fallback")
    return app
