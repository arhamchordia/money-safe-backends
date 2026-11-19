"""Part 4: a payment is a state machine, not a boolean.

    created --> pending --> captured --> refunded
       |           |
       +--> failed <+       (created --> captured is allowed: providers may skip pending)

Every change is checked against the allowed transitions, recorded in an
append-only audit table, written to the ledger when money actually moves,
and announced through the outbox, all in one transaction.
"""

from __future__ import annotations

from typing import Any

from .db import Connection, transaction
from .ledger import Posting, balance, lock_account, open_shop, post_entry, shop_wallet
from .money import split_fee
from .outbox import enqueue
from .wallet import InsufficientFunds

# MoneySafe's own platform fee, in basis points (1 bps = 0.01%). This is NOT MDR.
# UPI MDR is set by NPCI and is zero for small P2PM merchants, so the default
# is 0. A platform fee is a business decision; configure it per deployment.
DEFAULT_PLATFORM_FEE_BPS = 0

TRANSITIONS: dict[str, set[str]] = {
    "created": {"pending", "captured", "failed"},
    "pending": {"captured", "failed"},
    "captured": {"refunded"},
    "failed": set(),
    "refunded": set(),
}

# How far along each status is. Used to spot late, out-of-order webhooks.
RANK = {"created": 0, "pending": 1, "captured": 2, "failed": 2, "refunded": 3}


class IllegalTransition(Exception):
    pass


class ConcurrentModification(Exception):
    pass


class PaymentNotFound(LookupError):
    pass


def create_payment(conn: Connection, shop_id: int, amount_paise: int,
                   fee_bps: int = DEFAULT_PLATFORM_FEE_BPS, provider_ref: str | None = None) -> int:
    fee, _ = split_fee(amount_paise, fee_bps)
    with transaction(conn):
        open_shop(conn, shop_id)
        return conn.execute(
            "INSERT INTO payments (shop_id, amount_paise, fee_paise, status, provider_ref) "
            "VALUES (%s, %s, %s, 'created', %s) RETURNING id",
            (shop_id, amount_paise, fee, provider_ref),
        ).fetchone()["id"]


def get_payment(conn: Connection, payment_id: int, for_update: bool = False) -> dict:
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute("SELECT * FROM payments WHERE id = %s" + lock, (payment_id,)).fetchone()
    if row is None:
        raise PaymentNotFound(payment_id)
    return row


def find_by_provider_ref(conn: Connection, provider_ref: str) -> dict | None:
    return conn.execute("SELECT * FROM payments WHERE provider_ref = %s", (provider_ref,)).fetchone()


def set_provider_ref(conn: Connection, payment_id: int, provider_ref: str) -> None:
    conn.execute("UPDATE payments SET provider_ref = %s WHERE id = %s AND provider_ref IS NULL",
                 (provider_ref, payment_id))


def transition(conn: Connection, payment_id: int, to_status: str,
               source: str) -> bool:
    """Move a payment to a new status. Returns False if it is already there.

    The payment row is locked with SELECT ... FOR UPDATE, so two webhooks for
    the same payment are applied one after the other. The version check is
    optimistic locking: a second guard that also works without row locks.
    """
    with transaction(conn):
        p = get_payment(conn, payment_id, for_update=True)
        if p["status"] == to_status:
            return False
        if to_status not in TRANSITIONS[p["status"]]:
            raise IllegalTransition(f"payment {payment_id}: {p['status']} -> {to_status}")

        updated = conn.execute(
            "UPDATE payments SET status = %s, version = version + 1 WHERE id = %s AND version = %s",
            (to_status, payment_id, p["version"]),
        ).rowcount
        if updated == 0:
            raise ConcurrentModification(f"payment {payment_id} changed underneath us")

        conn.execute(
            "INSERT INTO payment_events (payment_id, from_status, to_status, source) "
            "VALUES (%s, %s, %s, %s)",
            (payment_id, p["status"], to_status, source),
        )
        _move_money(conn, p, to_status)
        enqueue(conn, f"payment.{to_status}", {
            "payment_id": payment_id, "shop_id": p["shop_id"], "amount_paise": p["amount_paise"],
        })
    return True


def _move_money(conn: Connection, p: dict, to_status: str) -> None:
    wallet = shop_wallet(p["shop_id"])
    amount, fee = p["amount_paise"], p["fee_paise"]
    net = amount - fee

    if to_status == "captured":
        postings = [Posting("provider:clearing", amount), Posting(wallet, -net)]
        if fee:
            postings.append(Posting("moneysafe:fees", -fee))
        post_entry(conn, f"Capture of payment {p['id']}", postings)

    elif to_status == "refunded":
        # Full refund: the customer gets everything back, including the platform fee.
        lock_account(conn, wallet)  # no payout may spend this money while we check it
        if balance(conn, wallet) < net:
            raise InsufficientFunds(f"shop {p['shop_id']} cannot cover refund of payment {p['id']}")
        postings = [Posting("provider:clearing", -amount), Posting(wallet, net)]
        if fee:
            postings.append(Posting("moneysafe:fees", fee))
        post_entry(conn, f"Refund of payment {p['id']}", postings)


def history(conn: Connection, payment_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT from_status, to_status, source, at FROM payment_events "
        "WHERE payment_id = %s ORDER BY id", (payment_id,)
    ).fetchall()
    return [dict(r) for r in rows]
