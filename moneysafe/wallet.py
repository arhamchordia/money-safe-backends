"""Part 2: the double charge.

Three ways to debit a wallet. Only the first one is wrong, and it is the one
almost everyone writes first.

Run the live demo:  python -m scripts.double_charge_demo
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Callable

from .db import connect, transaction

# A slow fraud check between the read and the write. It does not create the
# bug; it only widens the window so the bug shows up every time in a demo.
FRAUD_CHECK_SECONDS = 0.005


class InsufficientFunds(Exception):
    pass


def set_balance(conn: sqlite3.Connection, wallet_id: int, balance_paise: int) -> None:
    conn.execute(
        "INSERT INTO wallets (id, balance_paise) VALUES (?, ?) "
        "ON CONFLICT (id) DO UPDATE SET balance_paise = excluded.balance_paise",
        (wallet_id, balance_paise),
    )


def get_balance(conn: sqlite3.Connection, wallet_id: int) -> int:
    return conn.execute("SELECT balance_paise FROM wallets WHERE id = ?", (wallet_id,)).fetchone()[0]


def debit_naive(wallet_id: int, amount_paise: int, db: str | None = None) -> None:
    """BROKEN: check-then-act with a gap. Two requests can both see enough money."""
    with closing(connect(db)) as conn:
        balance = get_balance(conn, wallet_id)
        if balance < amount_paise:
            raise InsufficientFunds
        time.sleep(FRAUD_CHECK_SECONDS)
        conn.execute("UPDATE wallets SET balance_paise = ? WHERE id = ?",
                     (balance - amount_paise, wallet_id))


def debit_locked(wallet_id: int, amount_paise: int, db: str | None = None) -> None:
    """FIX 1: take the write lock before reading. Others wait in BEGIN IMMEDIATE.

    Postgres: SELECT balance FROM wallets WHERE id = %s FOR UPDATE
    (locks one row instead of the whole database).
    """
    with closing(connect(db)) as conn, transaction(conn):
        balance = get_balance(conn, wallet_id)
        if balance < amount_paise:
            raise InsufficientFunds
        time.sleep(FRAUD_CHECK_SECONDS)
        conn.execute("UPDATE wallets SET balance_paise = balance_paise - ? WHERE id = ?",
                     (amount_paise, wallet_id))


def debit_atomic(wallet_id: int, amount_paise: int, db: str | None = None) -> None:
    """FIX 2: let the database check and write in one statement. No gap at all."""
    with closing(connect(db)) as conn:
        cur = conn.execute(
            "UPDATE wallets SET balance_paise = balance_paise - ? "
            "WHERE id = ? AND balance_paise >= ?",
            (amount_paise, wallet_id, amount_paise),
        )
        if cur.rowcount == 0:
            raise InsufficientFunds


DebitFn = Callable[..., None]


def hammer(debit_fn: DebitFn, wallet_id: int = 1, requests: int = 100,
           amount_paise: int = 1_000, workers: int = 50, db: str | None = None) -> int:
    """Fire many debits at once and return how many succeeded."""
    def attempt(_: int) -> bool:
        try:
            debit_fn(wallet_id, amount_paise, db=db)
            return True
        except InsufficientFunds:
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(attempt, range(requests)))
