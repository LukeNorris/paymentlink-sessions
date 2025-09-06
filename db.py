# db.py
# Tiny SQLite helpers for the app.
# Note: On Heroku the filesystem is ephemeral; this is fine for tests,
# but not for production persistence.

import sqlite3
from datetime import datetime
from typing import Optional, Tuple

DB_NAME = "payments.db"

def init_db() -> None:
    """Create the payments table if it doesn't exist."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                amount INTEGER,
                currency TEXT,
                reference TEXT UNIQUE,
                status TEXT,
                country TEXT,
                expires_at DATETIME
            )
        """)

def create_payment_record(
    payment_id: str,
    amount_minor: int,
    currency: str,
    reference: str,
    country: str,
    expires_at: datetime,
) -> None:
    """Insert a new payment row with initial status 'pending'."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            INSERT INTO payments (id, amount, currency, reference, status, country, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (payment_id, amount_minor, currency, reference, "pending", country, expires_at))

def get_payment_by_id(payment_id: str) -> Optional[Tuple]:
    """Fetch a single payment row by id."""
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.execute(
            "SELECT id, amount, currency, reference, status, country, expires_at FROM payments WHERE id = ?",
            (payment_id,),
        )
        return cur.fetchone()

def update_status_by_id(payment_id: str, new_status: str) -> None:
    """Update status by payment id."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE payments SET status = ? WHERE id = ?", (new_status, payment_id))

def update_status_by_reference(reference: str, new_status: str) -> None:
    """Update status by business reference (before the UUID suffix used for attempts)."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE payments SET status = ? WHERE reference = ?", (new_status, reference))
