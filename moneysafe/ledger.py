"""Part 1: a double-entry ledger that always balances.

Every movement of money is one journal entry with two or more postings that
sum to zero. Money never appears or vanishes; it moves between accounts.
Balances are computed from postings, never stored and edited.

Sign convention: positive = debit, negative = credit.
Asset and expense accounts grow with debits; liability and revenue accounts
grow with credits. A shop's wallet is a liability: MoneySafe owes it to the shop.

The rule is enforced twice: here in Python, and by a deferred constraint
trigger in Postgres that checks every entry at COMMIT.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Connection, transaction

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


def open_account(conn: Connection, account_id: str, kind: str, name: str) -> None:
    conn.execute(
        "INSERT INTO accounts (id, kind, name) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (account_id, kind, name),
    )


def open_shop(conn: Connection, shop_id: int) -> str:
    account_id = shop_wallet(shop_id)
    open_account(conn, account_id, "liability", f"Wallet of shop {shop_id}")
    return account_id


def lock_account(conn: Connection, account_id: str) -> None:
    """Lock one account row until the transaction ends.

    Balances are sums over postings, and SELECT ... FOR UPDATE cannot lock rows
    that do not exist yet. So anything that checks a balance and then spends it
    first locks the account row itself: every spender of that account queues here.
    """
    if conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)).fetchone() is None:
        raise UnknownAccount(account_id)


def post_entry(conn: Connection, memo: str, postings: list[Posting]) -> int:
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

    accounts = {p.account_id for p in postings}
    with transaction(conn):
        found = {r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE id = ANY(%s)", (list(accounts),))}
        missing = accounts - found
        if missing:
            raise UnknownAccount(", ".join(sorted(missing)))
        entry_id = conn.execute("INSERT INTO journal_entries (memo) VALUES (%s) RETURNING id",
                                (memo,)).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO postings (entry_id, account_id, amount_paise) VALUES (%s, %s, %s)",
                [(entry_id, p.account_id, p.amount_paise) for p in postings],
            )
    return entry_id


def balance(conn: Connection, account_id: str, as_of_entry: int | None = None) -> int:
    """Balance in the account's natural direction, optionally as of an earlier entry."""
    row = conn.execute("SELECT kind FROM accounts WHERE id = %s", (account_id,)).fetchone()
    if row is None:
        raise UnknownAccount(account_id)
    raw = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM postings "
        "WHERE account_id = %s AND (%s::bigint IS NULL OR entry_id <= %s::bigint)",
        (account_id, as_of_entry, as_of_entry),
    ).fetchone()["total"]
    return int(raw) if row["kind"] in DEBIT_NORMAL else -int(raw)


def trial_balance(conn: Connection) -> int:
    """Sum of every posting ever written. Must always be exactly 0."""
    return int(conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM postings").fetchone()["total"])


def unbalanced_entries(conn: Connection) -> list[int]:
    """Entry ids whose postings do not sum to zero. Must always be empty."""
    rows = conn.execute(
        "SELECT entry_id FROM postings GROUP BY entry_id HAVING SUM(amount_paise) <> 0"
    ).fetchall()
    return [r["entry_id"] for r in rows]


def reverse_entry(conn: Connection, entry_id: int, memo: str) -> int:
    """Undo an entry by posting its exact opposite. The original stays visible."""
    rows = conn.execute(
        "SELECT account_id, amount_paise FROM postings WHERE entry_id = %s ORDER BY id", (entry_id,)
    ).fetchall()
    if not rows:
        raise ValueError(f"no such entry: {entry_id}")
    return post_entry(conn, memo, [Posting(r["account_id"], -r["amount_paise"]) for r in rows])
