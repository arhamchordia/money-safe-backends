"""A fake payment provider that runs in-process.

Labs containers have no outside network, so this module plays the provider.
Its behaviour follows what real Indian providers document:

  - Payout idempotency keys are remembered only for a window. RazorpayX
    documents 7 days; Stripe may prune keys after 24 hours. After the window,
    the same key creates a NEW payout. Configure `key_window` per lesson.
  - Reusing a key with a different body is rejected.
  - IMPS/UPI payouts can stay in `processing` for days ("deemed success")
    before reaching a final state: processed, reversed or failed.
  - A `processed` payout can later become `reversed`.
  - Webhooks are HMAC-SHA256 signed over the raw body, may arrive twice and
    may arrive out of order.
  - Captured payments are settled as ONE bank credit (with a UTR) covering
    many payments, net of the provider's fees and taxes, with a report that
    lists each payment under its settlement id.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Callable

SIGNATURE_HEADER = "X-MoneySafe-Signature"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def paise_to_rupee_str(paise: int) -> str:
    sign_ = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), 100)
    return f"{sign_}{rupees}.{rem:02d}"


class ProviderBadRequest(Exception):
    """The provider refused the request, e.g. a key reused with a different body."""


class ProviderTimeout(Exception):
    """The request may or may not have been processed: the response was lost."""


@dataclass
class ProviderPayout:
    id: str
    key: str
    request_hash: str
    amount_paise: int
    reference: str
    created_at: datetime
    status: str = "processing"
    deemed_success: bool = False


@dataclass
class BankCredit:
    """One line on MoneySafe's bank statement."""
    utr: str
    amount_paise: int


@dataclass
class FakeProvider:
    secret: str
    key_window: timedelta = timedelta(days=7)
    fee_bps: int = 0                 # provider's own fee on captured payments
    tax_bps_on_fee: int = 0          # tax charged on that fee
    clock: Callable[[], datetime] = utcnow
    amounts: dict[str, int] = field(default_factory=dict)            # ref -> paise (payments and payouts)
    payouts: dict[str, ProviderPayout] = field(default_factory=dict)  # payout id -> payout
    _keys: dict[str, ProviderPayout] = field(default_factory=dict)    # idempotency key -> payout
    _payment_ids: count = field(default_factory=lambda: count(1))
    _payout_ids: count = field(default_factory=lambda: count(1))
    _event_ids: count = field(default_factory=lambda: count(1))
    _settlement_ids: count = field(default_factory=lambda: count(1))
    lose_next_response: bool = False

    # Collections ---------------------------------------------------------------

    def create_payment(self, amount_paise: int) -> str:
        ref = f"pay_{next(self._payment_ids):06d}"
        self.amounts[ref] = amount_paise
        return ref

    # Payouts -------------------------------------------------------------------

    def create_payout(self, idempotency_key: str, amount_paise: int, reference: str) -> dict:
        """Create a payout, or return the existing one for a key still inside the window."""
        now = self.clock()
        request_hash = hashlib.sha256(f"{amount_paise}|{reference}".encode()).hexdigest()
        existing = self._keys.get(idempotency_key)
        if existing is not None and now - existing.created_at <= self.key_window:
            if existing.request_hash != request_hash:
                raise ProviderBadRequest("idempotency key reused with a different request body")
            payout = existing
        else:
            # Unknown key, or the key has expired: this is a brand new payout.
            payout = ProviderPayout(
                id=f"pout_{next(self._payout_ids):06d}", key=idempotency_key,
                request_hash=request_hash, amount_paise=amount_paise,
                reference=reference, created_at=now,
            )
            self._keys[idempotency_key] = payout
            self.payouts[payout.id] = payout
            self.amounts[payout.id] = amount_paise

        if self.lose_next_response:
            self.lose_next_response = False
            raise ProviderTimeout("request timed out; it may or may not have been processed")
        return self.fetch_payout(payout.id)

    def fetch_payout(self, payout_id: str) -> dict:
        p = self.payouts[payout_id]
        return {"id": p.id, "status": p.status, "amount_paise": p.amount_paise,
                "reference": p.reference, "deemed_success": p.deemed_success}

    def mark_deemed_success(self, payout_id: str) -> None:
        """NPCI did not confirm in time: the payout sits in processing for days."""
        self.payouts[payout_id].deemed_success = True

    def finish_payout(self, payout_id: str, status: str) -> None:
        if status not in {"processed", "reversed", "failed"}:
            raise ValueError(status)
        self.payouts[payout_id].status = status
        self.payouts[payout_id].deemed_success = False

    # Webhooks --------------------------------------------------------------------

    def event(self, ref: str, event_type: str, event_id: str | None = None) -> tuple[bytes, str]:
        """Build one signed webhook: (raw body, signature header value).

        Reuse `event_id` to simulate the provider delivering the same event twice.
        """
        payload = {
            "id": event_id or f"evt_{next(self._event_ids):06d}",
            "type": event_type,
            "data": {"ref": ref, "amount_paise": self.amounts[ref]},
        }
        if ref in self.payouts:
            payload["data"]["reference"] = self.payouts[ref].reference
        body = json.dumps(payload, separators=(",", ":")).encode()
        return body, sign(body, self.secret)

    # Settlement ------------------------------------------------------------------

    def fees_for(self, amount_paise: int) -> tuple[int, int]:
        fee = (amount_paise * self.fee_bps + 5_000) // 10_000
        tax = (fee * self.tax_bps_on_fee + 5_000) // 10_000
        return fee, tax

    def settle(self, refs: list[str], overrides: dict[str, int] | None = None,
               bank_short_by: int = 0) -> tuple[str, BankCredit]:
        """Settle captured payments: (settlement report CSV, the bank credit).

        Amounts in the report are rupee strings, like a real export.
        `overrides` plants a wrong amount on a line; `bank_short_by` makes the
        bank credit differ from the report, for reconciliation lessons.
        """
        overrides = overrides or {}
        settlement_id = f"setl_{next(self._settlement_ids):06d}"
        utr = f"UTR{next(self._settlement_ids):012d}"
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["settlement_id", "provider_ref", "amount", "fee", "tax"])
        net = 0
        for ref in refs:
            amount = overrides.get(ref, self.amounts[ref])
            fee, tax = self.fees_for(amount)
            net += amount - fee - tax
            writer.writerow([settlement_id, ref, paise_to_rupee_str(amount),
                             paise_to_rupee_str(fee), paise_to_rupee_str(tax)])
        return out.getvalue(), BankCredit(utr=utr, amount_paise=net - bank_short_by)
