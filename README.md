# ربات فروش VPN (Hiddify + Novinopay)

ربات تلگرام مینیمال: انتخاب پلن → لینک پرداخت [نوینوپی](https://novinopay.com/docs) → ساخت کاربر در Hiddify → ارسال لینک اشتراک در تلگرام.

## پیش‌نیاز

- پنل [Hiddify Manager](https://hiddify.com) با API فعال
- مرچنت [نوینوپی](https://novinopay.com/docs) (برای تست: `test`)
- توکن ربات از [@BotFather](https://t.me/BotFather)
- آدرس عمومی برای `PAYMENT_CALLBACK_BASE` (همان `callback_url` در پنل نوینو)

## تنظیم Novinopay

طبق [مستندات فنی](https://novinopay.com/docs):

1. **ایجاد تراکنش** — `POST https://api.novinopay.com/payment/ipg/v2/request`
2. **بازگشت از درگاه** — پارامترهای `PaymentStatus`, `Authority`, `InvoiceID` به `callback_url`
3. **تایید** — `POST https://api.novinopay.com/payment/ipg/v2/verification` (حداکثر ۱۰ دقیقه بعد از بازگشت)

در `.env`:

```bash
NOVINOPAY_MERCHANT_ID=test          # تست؛ برای عملیاتی مرچنت واقعی
PAYMENT_CALLBACK_BASE=https://bot.example.com
```

`callback_url` نهایی: `{PAYMENT_CALLBACK_BASE}/payment/callback`

## تنظیم Hiddify

1. **Settings → API** — `HIDDIFY_API_KEY`
2. **Admin Proxy Path** — `HIDDIFY_ADMIN_PATH`
3. **User Proxy Path** — `HIDDIFY_USER_PATH`
4. آدرس پنل — `HIDDIFY_BASE_URL`

## اجرا

```bash
cp .env.example .env
docker compose up -d --build
```

## پلن‌ها

فایل `plans.json` — مبلغ به **ریال**، حداقل ۱۰٬۰۰۰ (محدودیت نوینو).

## سرورها

برای **چند سرور**، فایل `servers.json` بساز (نمونه: `servers.json.example`):

```json
[
  {
    "id": "de",
    "title": "🇩🇪 آلمان",
    "base_url": "https://de.example.com",
    "admin_path": "...",
    "api_key": "...",
    "user_path": "..."
  }
]
```

اگر `servers.json` نباشد، از متغیرهای `HIDDIFY_*` در `.env` یک سرور ساخته می‌شود (رفتار قبلی).

- **یک سرور**: بعد از انتخاب پلن مستقیم لینک پرداخت
- **چند سرور**: پلن → انتخاب سرور → لینک پرداخت

در docker می‌توانی mount کنی:

```yaml
volumes:
  - ./servers.json:/app/servers.json:ro
```

## جریان کار

1. کاربر `/start` → انتخاب پلن
2. ربات تراکنش نوینو می‌سازد و `payment_url` را می‌فرستد
3. بعد از پرداخت، نوینو کاربر را با `PaymentStatus=OK` به callback برمی‌گرداند
4. ربات verify می‌کند، اکانت Hiddify می‌سازد، لینک اشتراک را در تلگرام می‌فرستد

## لاگ

```bash
docker compose logs -f vpn-bot
```

بعد از کلیک روی پلن باید ببینی:
`telegram callback data='buy:0'` و `on_buy handler`

## عیب‌یابی دکمه‌های پلن

1. **فقط یک instance** با همین توکن ربات اجرا شود (docker + اجرای محلی همزمان = callback گم می‌شود)
2. بعد از rebuild حتماً **دوباره `/start`** بزن — دکمه‌های پیام قدیمی کار نمی‌کنند
3. `PAYMENT_CALLBACK_BASE` فقط دامنه باشد، مثلاً `http://joorabino1.ir` (نه `/payment/callback`)
4. اگر HTTPS داری، ربات خودکار **webhook** می‌زند روی `{BASE}/telegram/webhook` — nginx باید POST را به پورت 8080 پاس بدهد

سطح لاگ با `LOG_LEVEL` (پیش‌فرض `INFO`، برای جزئیات بیشتر: `DEBUG`).

## ساختار

```
bot/
  payment.py       # Novinopay IPG v2
  main.py          # callback + polling
  hiddify.py
  telegram_bot.py
```
