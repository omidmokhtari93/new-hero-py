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


@dataclass(frozen=True)
class Server:
    id: str
    title: str
    base_url: str
    admin_path: str
    api_key: str
    user_path: str


def _load_plans() -> list[Plan]:
    path = Path(os.getenv("PLANS_FILE", "plans.json"))
    if not path.is_file():
        return [
            Plan("30d_50gb", "یک ماه — ۵۰ گیگ", 30, 50, 99_000),
        ]
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Plan(**p) for p in data]


def _load_servers() -> list[Server]:
    path = Path(os.getenv("SERVERS_FILE", "servers.json"))
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            Server(
                id=s["id"],
                title=s["title"],
                base_url=s["base_url"].rstrip("/"),
                admin_path=s["admin_path"].strip("/"),
                api_key=s["api_key"],
                user_path=s["user_path"].strip("/"),
            )
            for s in data
        ]

    base = os.getenv("HIDDIFY_BASE_URL", "").rstrip("/")
    if base:
        return [
            Server(
                id="default",
                title=os.getenv("HIDDIFY_SERVER_TITLE", "سرور ۱"),
                base_url=base,
                admin_path=os.environ["HIDDIFY_ADMIN_PATH"].strip("/"),
                api_key=os.environ["HIDDIFY_API_KEY"],
                user_path=os.environ["HIDDIFY_USER_PATH"].strip("/"),
            )
        ]

    raise RuntimeError("هیچ سروری تعریف نشده — servers.json یا متغیرهای HIDDIFY_*")


def _normalize_public_base(url: str) -> str:
    url = url.rstrip("/")
    for suffix in ("/payment/callback", "/callback"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


PLANS = _load_plans()
PLANS_BY_ID = {p.id: p for p in PLANS}

SERVERS = _load_servers()
SERVERS_BY_ID = {s.id: s for s in SERVERS}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

NOVINOPAY_MERCHANT_ID = os.getenv("NOVINOPAY_MERCHANT_ID", "test")
PAYMENT_CALLBACK_BASE = _normalize_public_base(os.environ["PAYMENT_CALLBACK_BASE"])

DB_PATH = os.getenv("DB_PATH", "data/orders.db")
