"""Part 1: a double-entry ledger that always balances.

Every movement of money is one journal entry with two or more postings that
sum to zero. Money never appears or vanishes; it moves between accounts.
Balances are computed from postings, never stored and edited.

Sign convention: positive = debit, negative = credit.
Asset and expense accounts grow with debits; liability and revenue accounts
grow with credits. A shop's wallet is a liability: MoneySafe owes it to the shop.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import transaction

DEBIT_NORMAL = {"asset", "expense"}


@dataclass(frozen=True)
class Posting:
    account_id: str
    amount_paise: int  # positive = debit, negative = credit


class UnbalancedEntry(ValueError):
    pass


class UnknownAccount(ValueError):
    pass


def shop_wallet(shop_id: int) -> str:
    return f"shop:{shop_id}:wallet"


def open_account(conn: sqlite3.Connection, account_id: str, kind: str, name: str) -> None:
    conn.execute(
        "INSERT INTO accounts (id, kind, name) VALUES (?, ?, ?) ON CONFLICT (id) DO NOTHING",
        (account_id, kind, name),
    )


def open_shop(conn: sqlite3.Connection, shop_id: int) -> str:
    account_id = shop_wallet(shop_id)
    open_account(conn, account_id, "liability", f"Wallet of shop {shop_id}")
    return account_id


def post_entry(conn: sqlite3.Connection, memo: str, postings: list[Posting]) -> int:
    """Write one balanced journal entry atomically and return its id."""
    if len(postings) < 2:
        raise UnbalancedEntry("an entry needs at least two postings")
    if any(not isinstance(p.amount_paise, int) or isinstance(p.amount_paise, bool)
           for p in postings):
        raise UnbalancedEntry("amounts must be integer paise")
    if any(p.amount_paise == 0 for p in postings):
        raise UnbalancedEntry("a posting cannot be zero")
    total = sum(p.amount_paise for p in postings)
    if total != 0:
        raise UnbalancedEntry(f"postings sum to {total}, not 0")

    with transaction(conn):
        for p in postings:
            if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (p.account_id,)).fetchone() is None:
                raise UnknownAccount(p.account_id)
        entry_id = conn.execute("INSERT INTO journal_entries (memo) VALUES (?)", (memo,)).lastrowid
        conn.executemany(
            "INSERT INTO postings (entry_id, account_id, amount_paise) VALUES (?, ?, ?)",
            [(entry_id, p.account_id, p.amount_paise) for p in postings],
        )
    return entry_id


def balance(conn: sqlite3.Connection, account_id: str, as_of_entry: int | None = None) -> int:
    """Balance in the account's natural direction, optionally as of an earlier entry."""
    row = conn.execute("SELECT kind FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise UnknownAccount(account_id)
    query = "SELECT COALESCE(SUM(amount_paise), 0) FROM postings WHERE account_id = ?"
    params: tuple = (account_id,)
    if as_of_entry is not None:
        query += " AND entry_id <= ?"
        params = (account_id, as_of_entry)
    raw = conn.execute(query, params).fetchone()[0]
    return raw if row["kind"] in DEBIT_NORMAL else -raw


def trial_balance(conn: sqlite3.Connection) -> int:
    """Sum of every posting ever written. Must always be exactly 0."""
    return conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM postings").fetchone()[0]


def unbalanced_entries(conn: sqlite3.Connection) -> list[int]:
    """Entry ids whose postings do not sum to zero. Must always be empty."""
    rows = conn.execute(
        "SELECT entry_id FROM postings GROUP BY entry_id HAVING SUM(amount_paise) <> 0"
    ).fetchall()
    return [r[0] for r in rows]


def reverse_entry(conn: sqlite3.Connection, entry_id: int, memo: str) -> int:
    """Undo an entry by posting its exact opposite. The original stays visible."""
    rows = conn.execute(
        "SELECT account_id, amount_paise FROM postings WHERE entry_id = ? ORDER BY id", (entry_id,)
    ).fetchall()
    if not rows:
        raise ValueError(f"no such entry: {entry_id}")
    return post_entry(conn, memo, [Posting(r["account_id"], -r["amount_paise"]) for r in rows])
