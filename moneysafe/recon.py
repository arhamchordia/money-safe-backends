"""Part 5: reconciliation, or "does our money match their money?"

How settlement really arrives (Razorpay documents this shape):
  - Captured payments are settled on a cycle (standard T+2 working days).
  - The bank shows ONE credit, with a UTR, covering many payments, net of the
    provider's fees and taxes.
  - A settlement report lists each payment under its settlement id.

So reconciliation has two halves:
  1. Line by line: every line in the report matches a payment we captured.
  2. The lump: report gross - fees - taxes == the credit on our bank statement.
A settlement is posted to the ledger only when both halves are clean.
Anything else is held for a human, with the reasons listed.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .db import transaction
from .ledger import Posting, post_entry, trial_balance
from .money import to_paise
from .provider import BankCredit


class SettlementNotClean(Exception):
    pass


@dataclass(frozen=True)
class SettlementLine:
    settlement_id: str
    provider_ref: str
    amount_paise: int
    fee_paise: int
    tax_paise: int


@dataclass
class ReconReport:
    settlement_id: str
    utr: str
    lines: list[SettlementLine]
    bank_credit_paise: int
    matched: dict[str, int] = field(default_factory=dict)               # ref -> paise
    amount_mismatch: dict[str, tuple[int, int]] = field(default_factory=dict)  # ref -> (ours, theirs)
    not_captured_by_us: dict[str, int] = field(default_factory=dict)    # they settled, we never captured
    already_settled: dict[str, int] = field(default_factory=dict)       # settled in an earlier settlement
    ledger_trial_balance: int = 0

    @property
    def gross_paise(self) -> int:
        return sum(line.amount_paise for line in self.lines)

    @property
    def fees_paise(self) -> int:
        return sum(line.fee_paise + line.tax_paise for line in self.lines)

    @property
    def expected_net_paise(self) -> int:
        return self.gross_paise - self.fees_paise

    @property
    def bank_difference_paise(self) -> int:
        return self.bank_credit_paise - self.expected_net_paise

    @property
    def problems(self) -> list[str]:
        out = []
        if self.amount_mismatch:
            out.append(f"{len(self.amount_mismatch)} line(s) with a different amount")
        if self.not_captured_by_us:
            out.append(f"{len(self.not_captured_by_us)} line(s) we never captured")
        if self.already_settled:
            out.append(f"{len(self.already_settled)} line(s) already settled before")
        if self.bank_difference_paise:
            out.append(f"bank credit differs from report by {self.bank_difference_paise} paise")
        if self.ledger_trial_balance:
            out.append("ledger does not balance")
        return out

    @property
    def is_clean(self) -> bool:
        return not self.problems


def parse_settlement(csv_text: str) -> list[SettlementLine]:
    """Read the provider's report (rupee strings) into lines in paise."""
    lines: list[SettlementLine] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(csv_text)):
        ref = row["provider_ref"]
        if ref in seen:
            raise ValueError(f"duplicate line for {ref} in settlement report")
        seen.add(ref)
        lines.append(SettlementLine(row["settlement_id"], ref, to_paise(row["amount"]),
                                    to_paise(row["fee"]), to_paise(row["tax"])))
    if len({line.settlement_id for line in lines}) > 1:
        raise ValueError("report mixes several settlements; reconcile one at a time")
    return lines


def reconcile(conn: sqlite3.Connection, lines: list[SettlementLine], bank: BankCredit) -> ReconReport:
    if not lines:
        raise ValueError("empty settlement report")
    report = ReconReport(settlement_id=lines[0].settlement_id, utr=bank.utr, lines=lines,
                         bank_credit_paise=bank.amount_paise,
                         ledger_trial_balance=trial_balance(conn))
    for line in lines:
        ref = line.provider_ref
        if conn.execute("SELECT 1 FROM settlement_items WHERE provider_ref = ?", (ref,)).fetchone():
            report.already_settled[ref] = line.amount_paise
            continue
        ours = conn.execute(
            "SELECT amount_paise FROM payments WHERE provider_ref = ? AND status = 'captured'", (ref,)
        ).fetchone()
        if ours is None:
            report.not_captured_by_us[ref] = line.amount_paise
        elif ours["amount_paise"] != line.amount_paise:
            report.amount_mismatch[ref] = (ours["amount_paise"], line.amount_paise)
        else:
            report.matched[ref] = line.amount_paise
    return report


def post_settlement(conn: sqlite3.Connection, report: ReconReport) -> int:
    """Record a clean settlement: clearing goes down, bank and provider fees go up."""
    if not report.is_clean:
        raise SettlementNotClean("; ".join(report.problems))
    with transaction(conn):
        postings = [
            Posting("bank:main", report.bank_credit_paise),
            Posting("provider:clearing", -report.gross_paise),
        ]
        if report.fees_paise:
            postings.append(Posting("provider:fees", report.fees_paise))
        entry_id = post_entry(conn, f"Settlement {report.settlement_id} (UTR {report.utr})", postings)
        # Primary keys on settlement_id, utr and provider_ref make a replay fail loudly.
        conn.execute(
            "INSERT INTO settlements (settlement_id, utr, gross_paise, fees_paise, net_paise, entry_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (report.settlement_id, report.utr, report.gross_paise, report.fees_paise,
             report.bank_credit_paise, entry_id),
        )
        conn.executemany("INSERT INTO settlement_items (provider_ref, settlement_id) VALUES (?, ?)",
                         [(ref, report.settlement_id) for ref in report.matched])
    return entry_id


def overdue_captures(conn: sqlite3.Connection, as_of: datetime,
                     expected_days: int = 2) -> dict[str, int]:
    """Captured payments still unsettled after the expected cycle.

    Uses calendar days for simplicity. Real cycles count working days and skip
    bank holidays, so production code needs a holiday calendar here.
    """
    rows = conn.execute(
        "SELECT p.provider_ref, p.amount_paise, MAX(e.at) AS captured_at FROM payments p "
        "JOIN payment_events e ON e.payment_id = p.id AND e.to_status = 'captured' "
        "LEFT JOIN settlement_items s ON s.provider_ref = p.provider_ref "
        "WHERE p.status = 'captured' AND s.provider_ref IS NULL GROUP BY p.id"
    ).fetchall()
    cutoff = as_of - timedelta(days=expected_days)
    return {r["provider_ref"]: r["amount_paise"] for r in rows
            if datetime.fromisoformat(r["captured_at"]) < cutoff}
