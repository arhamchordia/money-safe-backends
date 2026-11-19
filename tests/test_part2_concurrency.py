"""Part 2 Checks: correct under 100 concurrent requests."""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import pytest

from moneysafe.db import connect
from moneysafe.ledger import balance, shop_wallet, trial_balance
from moneysafe.payouts import create_payout
from moneysafe.wallet import (InsufficientFunds, debit_atomic, debit_locked, debit_naive,
                             get_balance, hammer, set_balance)

START, DEBIT = 50_000, 1_000  # Rs 500 wallet, Rs 10 debits -> exactly 50 can succeed


def _reset(db_path):
    with closing(connect(db_path)) as conn:
        set_balance(conn, 1, START)


def _final(db_path):
    with closing(connect(db_path)) as conn:
        return get_balance(conn, 1)


def test_naive_debit_double_charges(db_path):
    """The bug, proven: more debits succeed than the wallet can pay for."""
    _reset(db_path)
    successes = hammer(debit_naive, requests=100, amount_paise=DEBIT, db=db_path)
    final = _final(db_path)
    assert successes > START // DEBIT or final != START - successes * DEBIT


@pytest.mark.parametrize("debit_fn", [debit_locked, debit_atomic])
def test_fixed_debits_are_exact(db_path, debit_fn):
    _reset(db_path)
    successes = hammer(debit_fn, requests=100, amount_paise=DEBIT, db=db_path)
    assert successes == 50
    assert _final(db_path) == 0


@pytest.mark.parametrize("debit_fn", [debit_locked, debit_atomic])
def test_cannot_overdraw(db_path, debit_fn):
    _reset(db_path)
    with pytest.raises(InsufficientFunds):
        debit_fn(1, START + 1, db=db_path)
    assert _final(db_path) == START


def test_ledger_payouts_survive_a_stampede(db_path, conn, fund_shop):
    """The real service: 100 parallel payouts against a Rs 500 shop wallet."""
    fund_shop(conn, 7, START)

    def attempt(i: int) -> bool:
        with closing(connect(db_path)) as c:
            try:
                create_payout(c, shop_id=7, supplier_id=i, amount_paise=DEBIT)
                return True
            except InsufficientFunds:
                return False

    with ThreadPoolExecutor(max_workers=50) as pool:
        successes = sum(pool.map(attempt, range(100)))

    assert successes == 50
    assert balance(conn, shop_wallet(7)) == 0
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 50
    assert trial_balance(conn) == 0
