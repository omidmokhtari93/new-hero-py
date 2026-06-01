import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    days: int
    gb: int
    price_rial: int


def _load_plans() -> list[Plan]:
    path = Path(os.getenv("PLANS_FILE", "plans.json"))
    if not path.is_file():
        return [
            Plan("30d_50gb", "یک ماه — ۵۰ گیگ", 30, 50, 99_000),
        ]
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Plan(**p) for p in data]


def _normalize_public_base(url: str) -> str:
    url = url.rstrip("/")
    for suffix in ("/payment/callback", "/callback"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


PLANS = _load_plans()
PLANS_BY_ID = {p.id: p for p in PLANS}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

HIDDIFY_BASE_URL = os.environ["HIDDIFY_BASE_URL"].rstrip("/")
HIDDIFY_ADMIN_PATH = os.environ["HIDDIFY_ADMIN_PATH"].strip("/")
HIDDIFY_API_KEY = os.environ["HIDDIFY_API_KEY"]
HIDDIFY_USER_PATH = os.environ["HIDDIFY_USER_PATH"].strip("/")

NOVINOPAY_MERCHANT_ID = os.getenv("NOVINOPAY_MERCHANT_ID", "test")
PAYMENT_CALLBACK_BASE = _normalize_public_base(os.environ["PAYMENT_CALLBACK_BASE"])

DB_PATH = os.getenv("DB_PATH", "data/orders.db")
