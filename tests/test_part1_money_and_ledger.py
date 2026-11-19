"""Part 1 Checks: money is not a float, and the books always balance."""

from __future__ import annotations

import sqlite3

import pytest

from moneysafe.ledger import (Posting, UnbalancedEntry, UnknownAccount, balance, open_shop,
                             post_entry, reverse_entry, shop_wallet, trial_balance,
                             unbalanced_entries)
from moneysafe.money import InvalidAmount, format_inr, split_fee, to_paise


# Chapter 1.1: paise, not floats ------------------------------------------------

@pytest.mark.parametrize("rupees, paise", [
    ("0", 0), ("1", 100), ("12.5", 1250), ("12.50", 1250), ("0.01", 1),
    ("1,23,456.78", 12345678), (" 99.99 ", 9999), (7, 700),
])
def test_to_paise(rupees, paise):
    assert to_paise(rupees) == paise


def test_float_trap_is_avoided():
    assert 0.1 + 0.2 != 0.3
    assert to_paise("0.10") + to_paise("0.20") == to_paise("0.30")


@pytest.mark.parametrize("bad", [0.1, "12.345", "abc", "", "NaN", "Infinity", True])
def test_to_paise_rejects_ambiguous_input(bad):
    with pytest.raises(InvalidAmount):
        to_paise(bad)


@pytest.mark.parametrize("paise, text", [
    (0, "Rs 0.00"), (5, "Rs 0.05"), (100000, "Rs 1,000.00"),
    (12345678, "Rs 1,23,456.78"), (1234567890, "Rs 1,23,45,678.90"), (-150, "-Rs 1.50"),
])
def test_format_inr_uses_indian_grouping(paise, text):
    assert format_inr(paise) == text


@pytest.mark.parametrize("amount", [1, 99, 101, 10_000, 12_345, 99_999_999])
def test_split_fee_never_loses_a_paisa(amount):
    fee, rest = split_fee(amount, 200)
    assert fee + rest == amount
    assert fee >= 0 and rest >= 0


def test_split_fee_rounds_half_up():
    assert split_fee(25, 200) == (1, 24)    # 0.5 paise rounds up
    assert split_fee(10_000, 200) == (200, 9_800)


# Chapter 1.2 and 1.3: journal entries -------------------------------------------

def test_balanced_entry_is_written(conn):
    open_shop(conn, 42)
    entry = post_entry(conn, "Collection for shop 42, with a Rs 2 platform fee", [
        Posting("provider:clearing", 10_000),
        Posting(shop_wallet(42), -9_800),
        Posting("moneysafe:fees", -200),
    ])
    assert entry > 0
    assert balance(conn, shop_wallet(42)) == 9_800
    assert balance(conn, "moneysafe:fees") == 200
    assert balance(conn, "provider:clearing") == 10_000
    assert trial_balance(conn) == 0


@pytest.mark.parametrize("postings", [
    [Posting("provider:clearing", 100)],
    [Posting("provider:clearing", 100), Posting("bank:main", -99)],
    [Posting("provider:clearing", 0), Posting("bank:main", 0)],
])
def test_unbalanced_entries_are_rejected_and_write_nothing(conn, postings):
    with pytest.raises(UnbalancedEntry):
        post_entry(conn, "bad", postings)
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_unknown_account_rolls_back_everything(conn):
    with pytest.raises(UnknownAccount):
        post_entry(conn, "typo", [Posting("provider:clearing", 100), Posting("shop:999:walet", -100)])
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0


def test_float_amounts_are_rejected(conn):
    with pytest.raises(UnbalancedEntry):
        post_entry(conn, "float", [Posting("provider:clearing", 1.5), Posting("bank:main", -1.5)])


# Chapter 1.4: append-only books ---------------------------------------------------

def test_ledger_rows_cannot_be_edited_or_deleted(conn, fund_shop):
    fund_shop(conn, 1, 5_000)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE postings SET amount_paise = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM postings")


def test_mistakes_are_fixed_with_a_reversal(conn, fund_shop):
    fund_shop(conn, 1, 5_000)
    entry = conn.execute("SELECT MAX(id) FROM journal_entries").fetchone()[0]
    reverse_entry(conn, entry, "reverse wrong funding")
    assert balance(conn, shop_wallet(1)) == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 2  # history kept
    assert balance(conn, shop_wallet(1), as_of_entry=entry) == 5_000               # point in time
    assert trial_balance(conn) == 0
    assert unbalanced_entries(conn) == []
