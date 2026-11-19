"""Part 4 Checks: payouts that are never sent twice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from moneysafe import payouts, webhooks
from moneysafe.ledger import balance, shop_wallet, trial_balance
from moneysafe.outbox import pending
from moneysafe.provider import FakeProvider, ProviderBadRequest, ProviderTimeout

SECRET = "payout-secret"
T0 = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def provider(clock):
    # RazorpayX documents a 7-day key window; lessons can shorten it.
    return FakeProvider(secret=SECRET, key_window=timedelta(days=7), clock=clock)


@pytest.fixture
def payout(conn, fund_shop):
    fund_shop(conn, 1, 10_000)
    return payouts.create_payout(conn, 1, 9, 4_000)["payout_id"]


def test_queued_payout_holds_the_money(conn, payout):
    assert payouts.get_payout(conn, payout)["status"] == "queued"
    assert balance(conn, shop_wallet(1)) == 6_000           # shop cannot spend it again
    assert balance(conn, "payouts:in_flight") == 4_000
    assert trial_balance(conn) == 0


def test_full_happy_path(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    assert payouts.get_payout(conn, payout)["status"] == "processing"
    assert balance(conn, "payouts:in_flight") == 0
    ref = payouts.get_payout(conn, payout)["provider_ref"]
    provider.finish_payout(ref, "processed")
    assert webhooks.handle(conn, *provider.event(ref, "payout.processed"), SECRET) == "applied"
    assert [h["to_status"] for h in payouts.history(conn, payout)] == ["queued", "processing", "processed"]
    assert trial_balance(conn) == 0


def test_submit_twice_sends_once(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    payouts.submit(conn, provider, payout)
    assert len(provider.payouts) == 1


def test_timeout_then_retry_uses_the_same_key(conn, provider, payout):
    """The webinar hook, done right: the retry cannot create a second payout."""
    provider.lose_next_response = True
    with pytest.raises(ProviderTimeout):
        payouts.submit(conn, provider, payout)            # provider DID create it; we never heard
    assert payouts.get_payout(conn, payout)["status"] == "queued"
    payouts.submit(conn, provider, payout)                # retry with the stored key
    assert len(provider.payouts) == 1                     # still one payout at the provider
    assert payouts.get_payout(conn, payout)["status"] == "processing"


def test_a_new_key_per_attempt_pays_twice(provider):
    """The bug, proven against the provider: fresh keys mean fresh payouts."""
    provider.create_payout("attempt-1", 4_000, "payout-1")
    provider.create_payout("attempt-2", 4_000, "payout-1")
    assert len(provider.payouts) == 2


def test_provider_rejects_same_key_different_body(provider):
    provider.create_payout("key-1", 4_000, "payout-1")
    with pytest.raises(ProviderBadRequest):
        provider.create_payout("key-1", 5_000, "payout-1")


def test_expired_key_creates_a_new_payout_at_the_provider(provider, clock):
    provider.create_payout("key-1", 4_000, "payout-1")
    clock.advance(days=8)
    provider.create_payout("key-1", 4_000, "payout-1")
    assert len(provider.payouts) == 2


def test_retry_near_key_expiry_is_refused_and_alerts(conn, provider, clock, payout):
    provider.lose_next_response = True
    with pytest.raises(ProviderTimeout):
        payouts.submit(conn, provider, payout)
    clock.advance(days=7)                                  # inside the 1-hour safety margin
    with pytest.raises(payouts.ManualReviewRequired):
        payouts.submit(conn, provider, payout)
    assert len(provider.payouts) == 1
    assert any(r["topic"] == "ops.manual_review" for r in pending(conn))


def test_stuck_payout_is_synced_not_resent(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    ref = payouts.get_payout(conn, payout)["provider_ref"]
    provider.mark_deemed_success(ref)                      # sits in processing for days
    result = payouts.sync(conn, provider, payout)
    assert result["status"] == "processing" and result["remote"]["deemed_success"] is True
    provider.finish_payout(ref, "processed")
    assert payouts.sync(conn, provider, payout)["status"] == "processed"
    assert len(provider.payouts) == 1


def test_processed_then_reversed_returns_the_money(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    ref = payouts.get_payout(conn, payout)["provider_ref"]
    webhooks.handle(conn, *provider.event(ref, "payout.processed"), SECRET)
    assert webhooks.handle(conn, *provider.event(ref, "payout.reversed"), SECRET) == "applied"
    assert balance(conn, shop_wallet(1)) == 10_000
    assert trial_balance(conn) == 0


def test_failed_before_sending_releases_the_hold(conn, payout):
    payouts.transition(conn, payout, "failed", source="ops")
    assert balance(conn, shop_wallet(1)) == 10_000
    assert balance(conn, "payouts:in_flight") == 0


def test_webhook_after_lost_response_finds_the_payout(conn, provider, payout):
    provider.lose_next_response = True
    with pytest.raises(ProviderTimeout):
        payouts.submit(conn, provider, payout)
    ref = next(iter(provider.payouts))
    assert webhooks.handle(conn, *provider.event(ref, "payout.processed"), SECRET) == "applied"
    p = payouts.get_payout(conn, payout)
    assert (p["status"], p["provider_ref"]) == ("processed", ref)
    assert trial_balance(conn) == 0


def test_late_initiated_after_processed_is_stale(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    ref = payouts.get_payout(conn, payout)["provider_ref"]
    webhooks.handle(conn, *provider.event(ref, "payout.processed"), SECRET)
    assert webhooks.handle(conn, *provider.event(ref, "payout.initiated"), SECRET) == "stale"


def test_reversed_after_failed_needs_a_human(conn, provider, payout):
    payouts.submit(conn, provider, payout)
    ref = payouts.get_payout(conn, payout)["provider_ref"]
    webhooks.handle(conn, *provider.event(ref, "payout.failed"), SECRET)
    assert webhooks.handle(conn, *provider.event(ref, "payout.reversed"), SECRET) == "needs_review"
    assert balance(conn, shop_wallet(1)) == 10_000         # money returned exactly once
