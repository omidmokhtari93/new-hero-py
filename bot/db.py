import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from bot.config import DB_PATH

log = logging.getLogger(__name__)


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                amount_rial INTEGER NOT NULL,
                authority TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                hiddify_uuid TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    log.info("database ready path=%s", DB_PATH)


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_order(telegram_id: int, plan_id: str, amount_rial: int) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO orders (telegram_id, plan_id, amount_rial) VALUES (?, ?, ?)",
            (telegram_id, plan_id, amount_rial),
        )
        order_id = int(cur.lastrowid)
    log.info(
        "order created id=%s telegram_id=%s plan=%s amount=%s",
        order_id,
        telegram_id,
        plan_id,
        amount_rial,
    )
    return order_id


def set_authority(order_id: int, authority: str) -> None:
    with _conn() as c:
        c.execute("UPDATE orders SET authority = ? WHERE id = ?", (authority, order_id))
    log.info("order %s authority set=%s", order_id, authority)


def get_order_by_authority(authority: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE authority = ?", (authority,)).fetchone()
        return dict(row) if row else None


def mark_paid(order_id: int, hiddify_uuid: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE orders SET status = 'paid', hiddify_uuid = ? WHERE id = ?",
            (hiddify_uuid, order_id),
        )
    log.info("order %s marked paid hiddify_uuid=%s", order_id, hiddify_uuid)


def mark_failed(order_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE orders SET status = 'failed' WHERE id = ?", (order_id,))
    log.warning("order %s marked failed", order_id)
