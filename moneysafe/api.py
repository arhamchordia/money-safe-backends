"""Part 6: MoneySafe as an HTTP service.

Run it:   uvicorn moneysafe.api:create_app --factory --reload
Docs:     http://localhost:8000/docs

Endpoints are plain `def`, so FastAPI runs each one in a thread pool and each
request opens its own Postgres connection (set MONEYSAFE_DSN).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import ledger, payments, payouts, webhooks
from .db import connect, init_db
from .idempotency import IdempotencyConflict
from .provider import SIGNATURE_HEADER, FakeProvider, ProviderTimeout
from .wallet import InsufficientFunds


class PaymentIn(BaseModel):
    amount_paise: int = Field(gt=0, description="Amount in paise. Rs 100 = 10000")


class PayoutIn(BaseModel):
    shop_id: int
    supplier_id: int
    amount_paise: int = Field(gt=0)


def create_app(dsn: str | None = None, webhook_secret: str | None = None,
               platform_fee_bps: int | None = None, provider: FakeProvider | None = None) -> FastAPI:
    secret = webhook_secret or os.environ.get("MONEYSAFE_WEBHOOK_SECRET", "dev-secret-change-me")
    fee_bps = (platform_fee_bps if platform_fee_bps is not None
               else int(os.environ.get("MONEYSAFE_PLATFORM_FEE_BPS", payments.DEFAULT_PLATFORM_FEE_BPS)))
    provider = provider or FakeProvider(secret=secret)

    with connect(dsn) as conn:
        init_db(conn)

    app = FastAPI(title="MoneySafe", description="Money-safe payments backend for kirana shops")
    app.state.provider = provider

    def db():
        return connect(dsn)

    # Collections -------------------------------------------------------------------

    @app.post("/shops/{shop_id}/payments", status_code=201)
    def create_payment(shop_id: int, body: PaymentIn) -> dict[str, Any]:
        ref = provider.create_payment(body.amount_paise)
        with db() as conn:
            payment_id = payments.create_payment(conn, shop_id, body.amount_paise,
                                                 fee_bps=fee_bps, provider_ref=ref)
            payments.transition(conn, payment_id, "pending", source="api")
            p = payments.get_payment(conn, payment_id)
        return {"payment_id": payment_id, "provider_ref": ref, "status": p["status"],
                "amount_paise": p["amount_paise"], "platform_fee_paise": p["fee_paise"]}

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: int) -> dict[str, Any]:
        with db() as conn:
            try:
                p = payments.get_payment(conn, payment_id)
            except payments.PaymentNotFound:
                raise HTTPException(404, "payment not found")
            return {**dict(p), "history": payments.history(conn, payment_id)}

    @app.post("/payments/{payment_id}/refund")
    def refund(payment_id: int) -> dict[str, Any]:
        with db() as conn:
            try:
                changed = payments.transition(conn, payment_id, "refunded", source="api")
            except payments.PaymentNotFound:
                raise HTTPException(404, "payment not found")
            except payments.IllegalTransition as exc:
                raise HTTPException(409, str(exc))
            except InsufficientFunds as exc:
                raise HTTPException(402, str(exc))
        return {"payment_id": payment_id, "status": "refunded", "changed": changed}

    # Payouts -------------------------------------------------------------------------

    def _payout_view(conn, payout_id: int) -> dict[str, Any]:
        p = payouts.get_payout(conn, payout_id)
        return {"payout_id": p["id"], "shop_id": p["shop_id"], "supplier_id": p["supplier_id"],
                "amount_paise": p["amount_paise"], "status": p["status"],
                "provider_ref": p["provider_ref"]}

    @app.post("/payouts", status_code=201)
    def create_payout(body: PayoutIn,
                      idempotency_key: str = Header(..., min_length=8, max_length=128)) -> dict[str, Any]:
        with db() as conn:
            try:
                created, _replayed = payouts.create_payout_idempotent(
                    conn, idempotency_key, body.shop_id, body.supplier_id, body.amount_paise)
            except IdempotencyConflict as exc:
                raise HTTPException(422, str(exc))
            except InsufficientFunds as exc:
                raise HTTPException(402, str(exc))
            try:
                payouts.submit(conn, provider, created["payout_id"])
            except ProviderTimeout:
                pass  # stays queued; a retry or webhook resolves it, always with the same provider key
            # Like RazorpayX, a repeat returns the payout's LATEST state, not a stale copy.
            return _payout_view(conn, created["payout_id"])

    @app.get("/payouts/{payout_id}")
    def get_payout(payout_id: int) -> dict[str, Any]:
        with db() as conn:
            try:
                return {**_payout_view(conn, payout_id), "history": payouts.history(conn, payout_id)}
            except payouts.PayoutNotFound:
                raise HTTPException(404, "payout not found")

    @app.post("/payouts/{payout_id}/sync")
    def sync_payout(payout_id: int) -> dict[str, Any]:
        """For a "stuck" payout: ask the provider. Never sends money."""
        with db() as conn:
            try:
                return payouts.sync(conn, provider, payout_id)
            except payouts.PayoutNotFound:
                raise HTTPException(404, "payout not found")

    @app.post("/payouts/{payout_id}/retry")
    def retry_payout(payout_id: int) -> dict[str, Any]:
        """Re-submit a payout whose submit call failed. Same provider key, inside its window."""
        with db() as conn:
            try:
                payouts.submit(conn, provider, payout_id)
            except payouts.PayoutNotFound:
                raise HTTPException(404, "payout not found")
            except payouts.ManualReviewRequired as exc:
                raise HTTPException(409, str(exc))
            except ProviderTimeout:
                raise HTTPException(504, "provider timed out; payout still queued, safe to retry")
            return _payout_view(conn, payout_id)

    # Webhooks and health ---------------------------------------------------------------

    @app.post("/webhooks/provider")
    async def provider_webhook(request: Request) -> dict[str, str]:
        body = await request.body()  # the raw bytes: the signature covers these exactly
        signature = request.headers.get(SIGNATURE_HEADER, "")
        with db() as conn:
            try:
                outcome = webhooks.handle(conn, body, signature, secret)
            except webhooks.InvalidSignature:
                raise HTTPException(401, "invalid signature")
        # 200 for every validly signed event, even duplicates, or the provider keeps retrying.
        return {"outcome": outcome}

    @app.get("/shops/{shop_id}/balance")
    def shop_balance(shop_id: int) -> dict[str, Any]:
        with db() as conn:
            try:
                paise = ledger.balance(conn, ledger.shop_wallet(shop_id))
            except ledger.UnknownAccount:
                raise HTTPException(404, "shop not found")
        return {"shop_id": shop_id, "balance_paise": paise}

    @app.get("/health/invariants")
    def invariants() -> dict[str, Any]:
        with db() as conn:
            total = ledger.trial_balance(conn)
            broken = ledger.unbalanced_entries(conn)
        if total != 0 or broken:
            raise HTTPException(500, {"trial_balance": total, "unbalanced_entries": broken})
        return {"ok": True, "trial_balance": total}

    return app
