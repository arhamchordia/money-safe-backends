"""Part 4: the transactional outbox.

Problem: after a payment is captured we must SMS the shop owner. If we send
the SMS inside the request and then the database commit fails, the shop is
told about money it does not have. If we commit first and then crash before
sending, the shop is never told.

Fix: write the message to an `outbox` table in the SAME transaction as the
payment change. Workers send pending messages later. Delivery is
at-least-once, so handlers must tolerate duplicates (dedupe on message id).
"""

from __future__ import annotations

from typing import Any, Callable

from psycopg.types.json import Jsonb

from .db import Connection, transaction

MAX_ATTEMPTS = 5

Handler = Callable[[int, str, dict[str, Any]], None]  # (message_id, topic, payload)


def enqueue(conn: Connection, topic: str, payload: dict[str, Any]) -> int:
    """Call this inside the business transaction, never after it."""
    return conn.execute("INSERT INTO outbox (topic, payload) VALUES (%s, %s) RETURNING id",
                        (topic, Jsonb(payload))).fetchone()["id"]


def pending(conn: Connection) -> list[dict]:
    return conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id").fetchall()


def process_one(conn: Connection, handler: Handler, max_attempts: int = MAX_ATTEMPTS,
                skip: list[int] | None = None) -> int | None:
    """Claim and try to deliver one pending message. Returns its id, or None if none is free.

    FOR UPDATE SKIP LOCKED lets many workers run at once: each one skips rows
    another worker has already claimed, so no message is handled twice at the
    same moment. The row stays locked while the handler runs.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT id, topic, payload, attempts FROM outbox "
            "WHERE status = 'pending' AND NOT (id = ANY(%s)) "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED", (skip or [],)
        ).fetchone()
        if row is None:
            return None
        try:
            handler(row["id"], row["topic"], row["payload"])
        except Exception as exc:  # noqa: BLE001 - any handler failure is retried
            attempts = row["attempts"] + 1
            conn.execute(
                "UPDATE outbox SET attempts = %s, status = %s, last_error = %s WHERE id = %s",
                (attempts, "dead" if attempts >= max_attempts else "pending", repr(exc), row["id"]),
            )
            return row["id"]
        conn.execute("UPDATE outbox SET status = 'done', attempts = attempts + 1 WHERE id = %s",
                     (row["id"],))
    return row["id"]


def process_batch(conn: Connection, handler: Handler, limit: int = 10,
                  max_attempts: int = MAX_ATTEMPTS) -> int:
    """Try each pending message at most once. Returns how many were tried."""
    tried: list[int] = []
    while len(tried) < limit:
        message_id = process_one(conn, handler, max_attempts, skip=tried)
        if message_id is None:
            break
        tried.append(message_id)
    return len(tried)
