import logging
import random
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
                server_id TEXT NOT NULL DEFAULT 'default',
                amount_rial INTEGER NOT NULL,
                authority TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                hiddify_uuid TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_active_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        cols = {row[1] for row in c.execute("PRAGMA table_info(orders)")}
        if "server_id" not in cols:
            c.execute(
                "ALTER TABLE orders ADD COLUMN server_id TEXT NOT NULL DEFAULT 'default'"
            )
            log.info("database migrated: added server_id column")
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


def create_order(telegram_id: int, plan_id: str, server_id: str, amount_rial: int) -> int:
    with _conn() as c:
        # Generate a unique random order ID
        while True:
            order_id = random.randint(111111, 999999)
            exists = c.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not exists:
                break
        
        c.execute(
            "INSERT INTO orders (id, telegram_id, plan_id, server_id, amount_rial) VALUES (?, ?, ?, ?, ?)",
            (order_id, telegram_id, plan_id, server_id, amount_rial),
        )
    log.info(
        "order created id=%s telegram_id=%s plan=%s server=%s amount=%s",
        order_id,
        telegram_id,
        plan_id,
        server_id,
        amount_rial,
    )
    return order_id


def get_order(order_id: int):
    with _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
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


def update_order_plan(order_id: int, plan_id: str, amount_rial: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE orders SET plan_id = ?, amount_rial = ? WHERE id = ?",
            (plan_id, amount_rial, order_id),
        )
    log.info("order %s updated plan=%s amount=%s", order_id, plan_id, amount_rial)


def get_user_orders(telegram_id: int, limit: int = 5) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE telegram_id = ? AND status = 'paid' ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_all_users() -> list[int]:
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT telegram_id FROM orders").fetchall()
        return [row[0] for row in rows]


def get_all_orders_paginated(limit: int, offset: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def count_all_orders() -> int:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) FROM orders").fetchone()
        return row[0] if row else 0


def search_order_by_uuid(uuid: str):
    with _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE hiddify_uuid = ?", (uuid,)).fetchone()
        return dict(row) if row else None


def upsert_user(telegram_id: int, first_name: str, last_name: str = None, username: str = None) -> None:
    with _conn() as c:
        # First check if user exists
        existing_user = c.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existing_user:
            # Update existing user's info and last_active_at
            c.execute(
                """
                UPDATE users 
                SET first_name = ?, last_name = ?, username = ?, last_active_at = datetime('now') 
                WHERE telegram_id = ?
                """,
                (first_name, last_name, username, telegram_id),
            )
            log.info("user updated telegram_id=%s", telegram_id)
        else:
            # Insert new user
            c.execute(
                """
                INSERT INTO users (telegram_id, first_name, last_name, username) 
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, first_name, last_name, username),
            )
            log.info("user created telegram_id=%s", telegram_id)


def get_all_users() -> list[int]:
    with _conn() as c:
        # Get all users from users table
        users_from_users = c.execute("SELECT telegram_id FROM users").fetchall()
        # Get all unique users from orders table
        users_from_orders = c.execute("SELECT DISTINCT telegram_id FROM orders").fetchall()
        
        # Combine both lists and get unique ids
        all_user_ids = set()
        for row in users_from_users:
            all_user_ids.add(row[0])
        for row in users_from_orders:
            all_user_ids.add(row[0])
            
        return list(all_user_ids)
