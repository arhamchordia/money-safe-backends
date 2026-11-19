"""Part 4: webhooks you can trust.

Providers tell you about payments and payouts by calling your webhook URL.
Razorpay documents that webhooks are HMAC-SHA256 signed over the raw body,
that the same event can arrive more than once, and that events can arrive out
of order. So assume every webhook may be:
  - forged        -> verify the signature before reading anything
  - duplicated    -> remember event ids and process each one once
  - out of order  -> never move a record backwards
  - about something you do not know -> record it, acknowledge, investigate
"""

from __future__ import annotations

import hmac
import json
import sqlite3

from . import payments, payouts
from .db import transaction
from .provider import sign

PAYMENT_EVENTS = {
    "payment.pending": "pending",
    "payment.captured": "captured",
    "payment.failed": "failed",
}
PAYOUT_EVENTS = {
    "payout.initiated": "processing",
    "payout.processed": "processed",
    "payout.reversed": "reversed",
    "payout.failed": "failed",
}


class InvalidSignature(Exception):
    pass


def verify(body: bytes, signature: str, secret: str) -> None:
    # compare_digest takes the same time whether the first or last byte differs,
    # so an attacker cannot guess the signature byte by byte.
    if not signature or not hmac.compare_digest(sign(body, secret), signature):
        raise InvalidSignature("webhook signature does not match")


def handle(conn: sqlite3.Connection, body: bytes, signature: str, secret: str) -> str:
    """Process one webhook. Returns an outcome label. Raises only for bad signatures.

    Outcomes: applied, no_change, duplicate, stale, needs_review, unknown, ignored_type
    """
    verify(body, signature, secret)
    event = json.loads(body)
    event_id, event_type = event["id"], event["type"]

    with transaction(conn):
        inserted = conn.execute(
            "INSERT INTO webhook_events (event_id, type, payload, outcome) "
            "VALUES (?, ?, ?, 'processing') ON CONFLICT (event_id) DO NOTHING",
            (event_id, event_type, body.decode()),
        ).rowcount
        if not inserted:
            return "duplicate"

        if event_type in PAYMENT_EVENTS:
            outcome = _apply_payment(conn, PAYMENT_EVENTS[event_type], event["data"], event_type)
        elif event_type in PAYOUT_EVENTS:
            outcome = _apply_payout(conn, PAYOUT_EVENTS[event_type], event["data"], event_type)
        else:
            outcome = "ignored_type"
        conn.execute("UPDATE webhook_events SET outcome = ? WHERE event_id = ?", (outcome, event_id))
    return outcome


def _apply_payment(conn: sqlite3.Connection, target: str, data: dict, event_type: str) -> str:
    payment = payments.find_by_provider_ref(conn, data["ref"])
    if payment is None:
        return "unknown"
    # Use what is stored, not what you were sent: a mismatched amount is never "fixed" silently.
    if data.get("amount_paise") != payment["amount_paise"]:
        return "needs_review"
    if payments.RANK[target] < payments.RANK[payment["status"]]:
        return "stale"  # e.g. a late "pending" after we already know it was captured
    try:
        changed = payments.transition(conn, payment["id"], target, source=f"webhook:{event_type}")
    except payments.IllegalTransition:
        return "needs_review"  # e.g. "failed" after "captured": a human must look
    return "applied" if changed else "no_change"


def _apply_payout(conn: sqlite3.Connection, target: str, data: dict, event_type: str) -> str:
    payout = payouts.find_by_reference(conn, data["ref"], data.get("reference"))
    if payout is None:
        return "unknown"
    if data.get("amount_paise") != payout["amount_paise"]:
        return "needs_review"
    if payout["provider_ref"] is None:
        # Our submit call timed out, but the provider did create the payout.
        conn.execute("UPDATE payouts SET provider_ref = ? WHERE id = ?", (data["ref"], payout["id"]))
    return payouts.apply_remote_status(conn, payout["id"], target, source=f"webhook:{event_type}")
