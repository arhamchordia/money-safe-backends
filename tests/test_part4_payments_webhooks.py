"""Part 4 Checks: payment state machine, webhooks and the outbox."""

from __future__ import annotations

import json
import psycopg

import pytest

from conftest import scalar

from moneysafe import outbox, payments, webhooks
from moneysafe.ledger import balance, shop_wallet, trial_balance
from moneysafe.provider import FakeProvider

SECRET = "test-secret"


@pytest.fixture
def provider():
    return FakeProvider(secret=SECRET)


@pytest.fixture
def payment(conn, provider):
    """A Rs 100 payment for shop 42 with a 2% platform fee, pending at the provider."""
    ref = provider.create_payment(10_000)
    pid = payments.create_payment(conn, 42, 10_000, fee_bps=200, provider_ref=ref)
    payments.transition(conn, pid, "pending", source="test")
    return pid, ref


# Chapter 4.1: the state machine ------------------------------------------------------

def test_capture_moves_money_and_records_history(conn, payment):
    pid, _ = payment
    assert payments.transition(conn, pid, "captured", source="test") is True
    assert balance(conn, shop_wallet(42)) == 9_800      # Rs 100 minus the 2% platform fee
    assert balance(conn, "moneysafe:fees") == 200
    assert [h["to_status"] for h in payments.history(conn, pid)] == ["pending", "captured"]
    assert trial_balance(conn) == 0


def test_same_transition_twice_is_a_no_op(conn, payment):
    pid, _ = payment
    payments.transition(conn, pid, "captured", source="test")
    assert payments.transition(conn, pid, "captured", source="test") is False
    assert balance(conn, shop_wallet(42)) == 9_800      # not credited twice


@pytest.mark.parametrize("path", [["failed", "captured"], ["captured", "pending"], ["captured", "failed"]])
def test_illegal_transitions_are_refused(conn, payment, path):
    pid, _ = payment
    payments.transition(conn, pid, path[0], source="test")
    with pytest.raises(payments.IllegalTransition):
        payments.transition(conn, pid, path[1], source="test")


def test_refund_reverses_the_money(conn, payment):
    pid, _ = payment
    payments.transition(conn, pid, "captured", source="test")
    payments.transition(conn, pid, "refunded", source="test")
    assert balance(conn, shop_wallet(42)) == 0
    assert balance(conn, "moneysafe:fees") == 0
    assert balance(conn, "provider:clearing") == 0
    assert trial_balance(conn) == 0


def test_audit_log_is_append_only(conn, payment):
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM payment_events")


# Chapter 4.2: webhooks ----------------------------------------------------------------

def test_valid_webhook_captures_payment(conn, payment, provider):
    pid, ref = payment
    body, sig = provider.event(ref, "payment.captured")
    assert webhooks.handle(conn, body, sig, SECRET) == "applied"
    assert payments.get_payment(conn, pid)["status"] == "captured"


def test_forged_webhook_is_rejected(conn, payment, provider):
    pid, ref = payment
    body, _ = provider.event(ref, "payment.captured")
    with pytest.raises(webhooks.InvalidSignature):
        webhooks.handle(conn, body, "0" * 64, SECRET)
    tampered = body.replace(b"10000", b"99999")
    with pytest.raises(webhooks.InvalidSignature):
        webhooks.handle(conn, tampered, provider.event(ref, "payment.captured")[1], SECRET)
    assert payments.get_payment(conn, pid)["status"] == "pending"


def test_duplicate_webhook_is_processed_once(conn, payment, provider):
    _, ref = payment
    body, sig = provider.event(ref, "payment.captured", event_id="evt_dup")
    assert webhooks.handle(conn, body, sig, SECRET) == "applied"
    assert webhooks.handle(conn, body, sig, SECRET) == "duplicate"
    assert balance(conn, shop_wallet(42)) == 9_800


def test_late_pending_after_capture_is_stale(conn, provider):
    ref = provider.create_payment(10_000)
    pid = payments.create_payment(conn, 42, 10_000, fee_bps=200, provider_ref=ref)
    captured = provider.event(ref, "payment.captured")
    pending = provider.event(ref, "payment.pending")
    assert webhooks.handle(conn, *captured, SECRET) == "applied"   # arrives first
    assert webhooks.handle(conn, *pending, SECRET) == "stale"      # arrives late
    assert payments.get_payment(conn, pid)["status"] == "captured"


def test_failed_after_captured_needs_a_human(conn, payment, provider):
    pid, ref = payment
    webhooks.handle(conn, *provider.event(ref, "payment.captured"), SECRET)
    assert webhooks.handle(conn, *provider.event(ref, "payment.failed"), SECRET) == "needs_review"
    assert payments.get_payment(conn, pid)["status"] == "captured"


def test_unknown_payment_is_recorded_not_crashed(conn, provider):
    ref = provider.create_payment(500)
    assert webhooks.handle(conn, *provider.event(ref, "payment.captured"), SECRET) == "unknown"
    assert scalar(conn, "SELECT outcome FROM webhook_events") == "unknown"


# Chapter 4.3: the outbox ---------------------------------------------------------------

def test_outbox_written_with_the_change_and_delivered_later(conn, payment):
    pid, _ = payment
    payments.transition(conn, pid, "captured", source="test")
    topics = [r["topic"] for r in outbox.pending(conn)]
    assert topics == ["payment.pending", "payment.captured"]

    sent = []
    assert outbox.process_batch(conn, lambda mid, topic, data: sent.append((topic, data))) == 2
    assert sent[1] == ("payment.captured", {"payment_id": pid, "shop_id": 42, "amount_paise": 10_000})
    assert outbox.pending(conn) == []


def test_no_outbox_message_if_the_change_rolls_back(conn, payment):
    pid, _ = payment
    before = len(outbox.pending(conn))
    with pytest.raises(payments.IllegalTransition):
        payments.transition(conn, pid, "refunded", source="test")
    assert len(outbox.pending(conn)) == before


def test_failing_handler_is_retried_then_parked(conn, payment):
    def broken(mid, topic, data):
        raise ConnectionError("SMS gateway down")

    for _ in range(5):
        outbox.process_batch(conn, broken, max_attempts=5)
    row = conn.execute("SELECT status, attempts, last_error FROM outbox ORDER BY id LIMIT 1").fetchone()
    assert (row["status"], row["attempts"]) == ("dead", 5)
    assert "SMS gateway down" in row["last_error"]


def test_webhook_payload_is_stored_for_audit(conn, payment, provider):
    _, ref = payment
    body, sig = provider.event(ref, "payment.captured", event_id="evt_audit")
    webhooks.handle(conn, body, sig, SECRET)
    stored = scalar(conn, "SELECT payload FROM webhook_events WHERE event_id = 'evt_audit'")
    assert stored["data"]["ref"] == ref


def test_platform_fee_defaults_to_zero(conn, provider):
    ref = provider.create_payment(10_000)
    pid = payments.create_payment(conn, 7, 10_000, provider_ref=ref)
    payments.transition(conn, pid, "captured", source="test")
    assert balance(conn, shop_wallet(7)) == 10_000


def test_two_workers_never_take_the_same_message(dsn, conn):
    """FOR UPDATE SKIP LOCKED: parallel workers split the outbox between them."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from moneysafe.db import connect

    for i in range(40):
        outbox.enqueue(conn, "sms.send", {"n": i})
    seen: list[int] = []
    lock = threading.Lock()

    def handler(mid, topic, data):
        time.sleep(0.002)
        with lock:
            seen.append(mid)

    def worker(_):
        with connect(dsn) as c:
            return outbox.process_batch(c, handler, limit=100)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert len(seen) == 40 and len(set(seen)) == 40      # every message once, none twice
    assert outbox.pending(conn) == []
