"""Part 4: the transactional outbox.

Problem: after a payment is captured we must SMS the shop owner. If we send
the SMS inside the request and then the database commit fails, the shop is
told about money it does not have. If we commit first and then crash before
sending, the shop is never told.

Fix: write the message to an `outbox` table in the SAME transaction as the
payment change. A worker sends pending messages later. Delivery is
at-least-once, so handlers must tolerate duplicates (dedupe on message id).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from .db import transaction

MAX_ATTEMPTS = 5

Handler = Callable[[int, str, dict[str, Any]], None]  # (message_id, topic, payload)


def enqueue(conn: sqlite3.Connection, topic: str, payload: dict[str, Any]) -> int:
    """Call this inside the business transaction, never after it."""
    return conn.execute("INSERT INTO outbox (topic, payload) VALUES (?, ?)",
                        (topic, json.dumps(payload))).lastrowid


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id").fetchall()


def process_batch(conn: sqlite3.Connection, handler: Handler, limit: int = 10,
                  max_attempts: int = MAX_ATTEMPTS) -> int:
    """Deliver up to `limit` pending messages. Returns how many were delivered.

    This version assumes one worker. With several workers you must claim rows
    first (Postgres: SELECT ... FOR UPDATE SKIP LOCKED).
    """
    rows = conn.execute(
        "SELECT id, topic, payload, attempts FROM outbox WHERE status = 'pending' "
        "ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    delivered = 0
    for row in rows:
        try:
            handler(row["id"], row["topic"], json.loads(row["payload"]))
        except Exception as exc:  # noqa: BLE001 - any handler failure is retried
            attempts = row["attempts"] + 1
            status = "dead" if attempts >= max_attempts else "pending"
            with transaction(conn):
                conn.execute(
                    "UPDATE outbox SET attempts = ?, status = ?, last_error = ? WHERE id = ?",
                    (attempts, status, repr(exc), row["id"]),
                )
            continue
        with transaction(conn):
            conn.execute("UPDATE outbox SET status = 'done', attempts = attempts + 1 WHERE id = ?",
                         (row["id"],))
        delivered += 1
    return delivered
