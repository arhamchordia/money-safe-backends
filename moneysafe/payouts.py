"""Part 3 and 4: paying a shop's supplier.

The lifecycle follows real payout APIs (RazorpayX documents queued,
processing, processed, reversed and failed):

    queued --> processing --> processed --> reversed
      |            |   \\
      +--> failed <+    +--> reversed

Money on the ledger at each step:
    queued      shop wallet -> payouts:in_flight     (a hold: the shop cannot spend it twice)
    processing  payouts:in_flight -> bank:main       (the provider debits our account)
    processed   nothing moves                        (the supplier's bank confirmed)
    reversed    bank:main -> shop wallet             (money came back, even after "processed")
    failed      back to the shop wallet              (from wherever it was)

Rules this module teaches:
  1. The provider idempotency key is created ONCE with the payout and sent on
     every attempt. A new key per attempt means a new payout.
  2. A "stuck" payout is asked about (sync), never sent again. IMPS and UPI
     payouts can legitimately sit in processing for days.
  3. Provider keys expire. Past the window, a retry with the same key is a
     brand new payout, so we stop and ask a human instead.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from .db import transaction
from .idempotency import run_once
from .ledger import Posting, balance, post_entry, shop_wallet
from .outbox import enqueue
from .provider import FakeProvider
from .wallet import InsufficientFunds

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"processing", "failed"},
    "processing": {"processed", "reversed", "failed"},
    "processed": {"reversed"},
    "reversed": set(),
    "failed": set(),
}
RANK = {"queued": 0, "processing": 1, "processed": 2, "reversed": 3, "failed": 3}
TERMINAL = {"processed", "reversed", "failed"}  # processed is final for the provider, but can still reverse

# Stop retrying this long before the provider forgets our key.
SAFETY_MARGIN = timedelta(hours=1)


class IllegalPayoutTransition(Exception):
    pass


class PayoutNotFound(LookupError):
    pass


class ManualReviewRequired(Exception):
    """Retrying now could pay twice. A human must check with the provider."""


def get_payout(conn: sqlite3.Connection, payout_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM payouts WHERE id = ?", (payout_id,)).fetchone()
    if row is None:
        raise PayoutNotFound(payout_id)
    return row


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"payout_id": row["id"], "shop_id": row["shop_id"], "supplier_id": row["supplier_id"],
            "amount_paise": row["amount_paise"], "status": row["status"]}


def create_payout(conn: sqlite3.Connection, shop_id: int, supplier_id: int,
                  amount_paise: int) -> dict[str, Any]:
    """Queue a payout and hold the money. Nothing is sent to the provider yet.

    The balance check and the hold happen inside one write-locked transaction,
    so two payouts can never both spend the same rupee.
    """
    if amount_paise <= 0:
        raise ValueError("payout amount must be positive")
    wallet = shop_wallet(shop_id)
    with transaction(conn):
        available = balance(conn, wallet)
        if available < amount_paise:
            raise InsufficientFunds(f"shop {shop_id} has {available} paise, needs {amount_paise}")
        # Rule 1: one key per payout, created now, stored, reused on every attempt.
        provider_key = str(uuid.uuid4())
        payout_id = conn.execute(
            "INSERT INTO payouts (shop_id, supplier_id, amount_paise, provider_key) VALUES (?, ?, ?, ?)",
            (shop_id, supplier_id, amount_paise, provider_key),
        ).lastrowid
        post_entry(conn, f"Hold for payout {payout_id}", [
            Posting(wallet, amount_paise),                 # debit: MoneySafe owes the shop less
            Posting("payouts:in_flight", -amount_paise),   # credit: now owed to the payout
        ])
        conn.execute("INSERT INTO payout_events (payout_id, from_status, to_status, source) "
                     "VALUES (?, 'new', 'queued', 'api')", (payout_id,))
        enqueue(conn, "payout.queued", {"payout_id": payout_id})
        return _as_dict(get_payout(conn, payout_id))


def create_payout_idempotent(conn: sqlite3.Connection, key: str, shop_id: int, supplier_id: int,
                             amount_paise: int, store_failures: bool = False
                             ) -> tuple[dict[str, Any], bool]:
    request = {"shop_id": shop_id, "supplier_id": supplier_id, "amount_paise": amount_paise}
    return run_once(conn, key, request,
                    lambda c: create_payout(c, shop_id, supplier_id, amount_paise),
                    store_failures=(InsufficientFunds,) if store_failures else ())


def transition(conn: sqlite3.Connection, payout_id: int, to_status: str, source: str) -> bool:
    """Move a payout to a new status and move the money. False if already there."""
    with transaction(conn):
        p = get_payout(conn, payout_id)
        if p["status"] == to_status:
            return False
        if to_status not in TRANSITIONS[p["status"]]:
            raise IllegalPayoutTransition(f"payout {payout_id}: {p['status']} -> {to_status}")
        updated = conn.execute(
            "UPDATE payouts SET status = ?, version = version + 1 WHERE id = ? AND version = ?",
            (to_status, payout_id, p["version"]),
        ).rowcount
        if updated == 0:
            raise IllegalPayoutTransition(f"payout {payout_id} changed underneath us")
        conn.execute(
            "INSERT INTO payout_events (payout_id, from_status, to_status, source) VALUES (?, ?, ?, ?)",
            (payout_id, p["status"], to_status, source),
        )
        _move_money(conn, p, to_status)
        enqueue(conn, f"payout.{to_status}", {"payout_id": payout_id, "shop_id": p["shop_id"],
                                              "amount_paise": p["amount_paise"]})
    return True


def _move_money(conn: sqlite3.Connection, p: sqlite3.Row, to_status: str) -> None:
    amount, wallet, pid = p["amount_paise"], shop_wallet(p["shop_id"]), p["id"]
    if to_status == "processing":
        post_entry(conn, f"Payout {pid} sent to provider", [
            Posting("payouts:in_flight", amount), Posting("bank:main", -amount)])
    elif to_status == "failed" and p["status"] == "queued":
        post_entry(conn, f"Payout {pid} failed before sending: release hold", [
            Posting("payouts:in_flight", amount), Posting(wallet, -amount)])
    elif to_status in {"failed", "reversed"}:
        post_entry(conn, f"Payout {pid} {to_status}: money returned", [
            Posting("bank:main", amount), Posting(wallet, -amount)])


def submit(conn: sqlite3.Connection, provider: FakeProvider, payout_id: int,
           now: datetime | None = None) -> dict[str, Any]:
    """Send a queued payout to the provider. Safe to call again after a timeout.

    The attempt time is committed BEFORE calling the provider, so we always
    know when the provider's key window started, even if we crash mid-call.
    """
    now = now or provider.clock()
    p = get_payout(conn, payout_id)
    if p["status"] != "queued":
        return _as_dict(p)  # already submitted: never send again

    if p["first_submitted_at"] is None:
        with transaction(conn):
            conn.execute("UPDATE payouts SET first_submitted_at = ? "
                         "WHERE id = ? AND first_submitted_at IS NULL", (now.isoformat(), payout_id))
    else:
        started = datetime.fromisoformat(p["first_submitted_at"])
        if now - started > provider.key_window - SAFETY_MARGIN:
            with transaction(conn):
                enqueue(conn, "ops.manual_review", {
                    "payout_id": payout_id,
                    "reason": "provider idempotency window nearly over; check provider before any retry",
                })
            raise ManualReviewRequired(
                f"payout {payout_id}: first attempt at {started.isoformat()}, provider forgets keys "
                f"after {provider.key_window}; retrying now could pay twice")

    # May raise ProviderTimeout: the payout stays queued and the caller retries with the SAME key.
    remote = provider.create_payout(p["provider_key"], p["amount_paise"], reference=f"payout-{payout_id}")
    _record_provider_ref(conn, payout_id, remote["id"])
    transition(conn, payout_id, "processing", source="submit")
    return _as_dict(get_payout(conn, payout_id))


def _record_provider_ref(conn: sqlite3.Connection, payout_id: int, provider_ref: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE payouts SET provider_ref = ? WHERE id = ? AND provider_ref IS NULL",
                     (provider_ref, payout_id))


def sync(conn: sqlite3.Connection, provider: FakeProvider, payout_id: int) -> dict[str, Any]:
    """The right response to a "stuck" payout: ask the provider, never resend.

    A payout in deemed success stays in processing here too, until the provider
    reports a final status.
    """
    p = get_payout(conn, payout_id)
    if p["provider_ref"] is None:
        return {**_as_dict(p), "remote": None}
    remote = provider.fetch_payout(p["provider_ref"])
    apply_remote_status(conn, payout_id, remote["status"], source="sync")
    return {**_as_dict(get_payout(conn, payout_id)), "remote": remote}


def apply_remote_status(conn: sqlite3.Connection, payout_id: int, remote_status: str,
                        source: str) -> str:
    """Bring our payout up to the provider's status. Returns an outcome label."""
    with transaction(conn):
        current = get_payout(conn, payout_id)["status"]
        if remote_status == current:
            return "no_change"
        if RANK[remote_status] < RANK[current]:
            return "stale"
        if current == "queued" and remote_status != "failed":
            transition(conn, payout_id, "processing", source=source)  # we missed the submit response
            if remote_status == "processing":
                return "applied"
        try:
            transition(conn, payout_id, remote_status, source=source)
        except IllegalPayoutTransition:
            return "needs_review"
    return "applied"


def find_by_reference(conn: sqlite3.Connection, provider_ref: str, reference: str | None
                      ) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM payouts WHERE provider_ref = ?", (provider_ref,)).fetchone()
    if row is None and reference and reference.startswith("payout-"):
        row = conn.execute("SELECT * FROM payouts WHERE id = ?",
                           (int(reference.removeprefix("payout-")),)).fetchone()
    return row


def history(conn: sqlite3.Connection, payout_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT from_status, to_status, source, at FROM payout_events "
                        "WHERE payout_id = ? ORDER BY id", (payout_id,)).fetchall()
    return [dict(r) for r in rows]
