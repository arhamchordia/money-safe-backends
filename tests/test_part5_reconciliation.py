"""Part 5 Checks: every rupee matched or explained."""

from __future__ import annotations

import psycopg
from datetime import datetime, timedelta, timezone

import pytest

from moneysafe import payments
from moneysafe.ledger import balance, trial_balance
from moneysafe.provider import FakeProvider
from moneysafe.recon import (SettlementNotClean, overdue_captures, parse_settlement,
                            post_settlement, reconcile)


@pytest.fixture
def provider():
    # Provider fee and tax are configurable; these numbers are for the exercise only.
    return FakeProvider(secret="s", fee_bps=100, tax_bps_on_fee=1_000)


@pytest.fixture
def day(conn, provider):
    """Three captured payments and one still pending."""
    refs = []
    for shop, amount in [(1, 10_000), (2, 25_050), (3, 7_500)]:
        ref = provider.create_payment(amount)
        pid = payments.create_payment(conn, shop, amount, provider_ref=ref)
        payments.transition(conn, pid, "captured", source="test")
        refs.append(ref)
    pending_ref = provider.create_payment(4_000)
    payments.create_payment(conn, 4, 4_000, provider_ref=pending_ref)
    return refs, pending_ref


def test_report_is_parsed_into_paise(provider, day):
    refs, _ = day
    csv_text, _ = provider.settle(refs)
    lines = parse_settlement(csv_text)
    assert [(line.provider_ref, line.amount_paise) for line in lines] == [
        (refs[0], 10_000), (refs[1], 25_050), (refs[2], 7_500)]
    assert lines[1].fee_paise == 251 and lines[1].tax_paise == 25   # 1% of 25,050 rounded; 10% of that


def test_clean_settlement_is_posted_net_of_fees(conn, provider, day):
    refs, _ = day
    csv_text, bank = provider.settle(refs)
    report = reconcile(conn, parse_settlement(csv_text), bank)
    assert report.is_clean, report.problems
    assert report.gross_paise == 42_550
    assert bank.amount_paise == report.expected_net_paise
    post_settlement(conn, report)
    assert balance(conn, "bank:main") == bank.amount_paise
    assert balance(conn, "provider:fees") == report.fees_paise
    assert balance(conn, "provider:clearing") == 0
    assert trial_balance(conn) == 0


def test_bank_credit_short_is_caught(conn, provider, day):
    refs, _ = day
    csv_text, bank = provider.settle(refs, bank_short_by=50)
    report = reconcile(conn, parse_settlement(csv_text), bank)
    assert report.bank_difference_paise == -50
    with pytest.raises(SettlementNotClean, match="bank credit differs"):
        post_settlement(conn, report)
    assert balance(conn, "bank:main") == 0                    # nothing posted


def test_every_kind_of_line_problem_is_reported(conn, provider, day):
    refs, pending_ref = day
    csv_text, bank = provider.settle([refs[0], refs[1], pending_ref], overrides={refs[1]: 25_000})
    report = reconcile(conn, parse_settlement(csv_text), bank)
    assert report.matched == {refs[0]: 10_000}
    assert report.amount_mismatch == {refs[1]: (25_050, 25_000)}
    assert report.not_captured_by_us == {pending_ref: 4_000}
    assert not report.is_clean
    with pytest.raises(SettlementNotClean):
        post_settlement(conn, report)


def test_payment_settled_twice_is_caught(conn, provider, day):
    refs, _ = day
    csv_text, bank = provider.settle(refs)
    post_settlement(conn, reconcile(conn, parse_settlement(csv_text), bank))
    csv_again, bank_again = provider.settle(refs[:1])
    report = reconcile(conn, parse_settlement(csv_again), bank_again)
    assert report.already_settled == {refs[0]: 10_000}
    assert not report.is_clean


def test_replaying_a_posted_settlement_fails_loudly(conn, provider, day):
    refs, _ = day
    csv_text, bank = provider.settle(refs)
    report = reconcile(conn, parse_settlement(csv_text), bank)
    post_settlement(conn, report)
    with pytest.raises(psycopg.Error):
        post_settlement(conn, report)
    assert balance(conn, "bank:main") == bank.amount_paise    # the replay rolled back


def test_overdue_captures_are_flagged_after_the_cycle(conn, provider, day):
    refs, _ = day
    csv_text, bank = provider.settle(refs[:2])
    post_settlement(conn, reconcile(conn, parse_settlement(csv_text), bank))
    now = datetime.now(timezone.utc)
    assert overdue_captures(conn, as_of=now) == {}                         # within T+2
    assert overdue_captures(conn, as_of=now + timedelta(days=3)) == {refs[2]: 7_500}


def test_duplicate_line_in_report_is_an_error(provider, day):
    refs, _ = day
    csv_text, _ = provider.settle([refs[0], refs[0]])
    with pytest.raises(ValueError, match="duplicate"):
        parse_settlement(csv_text)
