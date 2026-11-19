"""Webinar demo: how a Rs 500 wallet pays out far more than Rs 500.

    python scripts/double_charge_demo.py

Fires 100 parallel debits of Rs 10 at a wallet holding Rs 500, three times:
once with the naive code, once with a write lock, once with an atomic update.
The correct result is exactly 50 successes and a final balance of Rs 0.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moneysafe.db import connect, init_db  # noqa: E402
from moneysafe.money import format_inr  # noqa: E402
from moneysafe.wallet import debit_atomic, debit_locked, debit_naive, get_balance, hammer, set_balance  # noqa: E402

START = 50_000   # Rs 500
DEBIT = 1_000    # Rs 10
REQUESTS = 100


def run(label: str, fn, db: str) -> None:
    with closing(connect(db)) as conn:
        set_balance(conn, 1, START)
    successes = hammer(fn, wallet_id=1, requests=REQUESTS, amount_paise=DEBIT, db=db)
    with closing(connect(db)) as conn:
        final = get_balance(conn, 1)
    paid_out = successes * DEBIT
    money_created = paid_out - (START - final)
    verdict = "CORRECT" if successes == START // DEBIT and final == 0 else "WRONG"
    print(f"\n{label}")
    print(f"  payments that went through : {successes} (expected {START // DEBIT})")
    print(f"  money paid out             : {format_inr(paid_out)}")
    print(f"  final wallet balance       : {format_inr(final)}")
    if money_created:
        print(f"  money that appeared from nowhere: {format_inr(money_created)}")
    print(f"  => {verdict}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "demo.db")
        with closing(connect(db)) as conn:
            init_db(conn)
        print(f"Wallet starts with {format_inr(START)}. {REQUESTS} parallel debits of {format_inr(DEBIT)}.")
        run("1. Naive: read, check, write", debit_naive, db)
        run("2. Fixed: BEGIN IMMEDIATE (write lock first)", debit_locked, db)
        run("3. Fixed: one atomic UPDATE ... WHERE balance >= amount", debit_atomic, db)


if __name__ == "__main__":
    main()
