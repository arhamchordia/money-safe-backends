"""Part 3 Checks: every money operation is safe to retry."""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import pytest

from moneysafe.db import connect
from moneysafe.idempotency import IdempotencyConflict, StoredFailure, fingerprint, run_once
from moneysafe.ledger import balance, shop_wallet
from moneysafe.payouts import create_payout_idempotent
from moneysafe.wallet import InsufficientFunds


def test_fingerprint_ignores_key_order():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_retry_returns_saved_response_without_paying_again(conn, fund_shop):
    fund_shop(conn, 1, 10_000)
    first, replayed1 = create_payout_idempotent(conn, "key-abc-123", 1, 9, 3_000)
    second, replayed2 = create_payout_idempotent(conn, "key-abc-123", 1, 9, 3_000)
    assert (replayed1, replayed2) == (False, True)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 1
    assert balance(conn, shop_wallet(1)) == 7_000


def test_same_key_different_request_is_rejected(conn, fund_shop):
    fund_shop(conn, 1, 10_000)
    create_payout_idempotent(conn, "key-abc-123", 1, 9, 3_000)
    with pytest.raises(IdempotencyConflict):
        create_payout_idempotent(conn, "key-abc-123", 1, 9, 4_000)
    assert balance(conn, shop_wallet(1)) == 7_000


def test_failed_work_does_not_burn_the_key(conn, fund_shop):
    fund_shop(conn, 1, 1_000)
    with pytest.raises(InsufficientFunds):
        create_payout_idempotent(conn, "key-retry-later", 1, 9, 5_000)
    fund_shop(conn, 1, 5_000)
    result, replayed = create_payout_idempotent(conn, "key-retry-later", 1, 9, 5_000)
    assert replayed is False and result["status"] == "queued"


def test_five_simultaneous_retries_pay_once(db_path, conn, fund_shop):
    fund_shop(conn, 1, 10_000)

    def attempt(_):
        with closing(connect(db_path)) as c:
            return create_payout_idempotent(c, "key-same-moment", 1, 9, 2_500)[0]

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(attempt, range(5)))

    assert all(r == responses[0] for r in responses)
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 1
    assert balance(conn, shop_wallet(1)) == 7_500


def test_run_once_rolls_back_work_and_key_together(conn):
    def boom(c):
        c.execute("INSERT INTO wallets (id, balance_paise) VALUES (99, 1)")
        raise RuntimeError("crash mid-request")

    with pytest.raises(RuntimeError):
        run_once(conn, "key-crash-001", {"x": 1}, boom)
    assert conn.execute("SELECT COUNT(*) FROM wallets WHERE id = 99").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0


def test_stripe_style_failures_are_replayed(conn, fund_shop):
    """With store_failures, a failure is saved under the key like Stripe does."""
    fund_shop(conn, 1, 1_000)
    with pytest.raises(StoredFailure) as first:
        create_payout_idempotent(conn, "key-stored-fail", 1, 9, 5_000, store_failures=True)
    assert first.value.replayed is False
    fund_shop(conn, 1, 5_000)                       # even after the cause is fixed...
    with pytest.raises(StoredFailure) as again:     # ...the same key replays the same failure
        create_payout_idempotent(conn, "key-stored-fail", 1, 9, 5_000, store_failures=True)
    assert again.value.replayed is True
    assert again.value.response["error"] == "InsufficientFunds"
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 0
