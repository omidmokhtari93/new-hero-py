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
    if not path.is_file():
        raise RuntimeError(f"فایل سرورها یافت نشد: {path}")
    
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


PLANS = _load_plans()
PLANS_BY_ID = {p.id: p for p in PLANS}

SERVERS = _load_servers()
SERVERS_BY_ID = {s.id: s for s in SERVERS}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
BOT_NAME = os.getenv("BOT_NAME", "HeroVPN")

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "3991553456"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@hero_support1")

DB_PATH = os.getenv("DB_PATH", "data/orders.db")
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "6"))
