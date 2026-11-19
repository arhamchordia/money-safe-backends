"""Part 6 Checks: the whole flow over HTTP."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from moneysafe.api import create_app
from moneysafe.provider import SIGNATURE_HEADER

SECRET = "api-test-secret"


@pytest.fixture
def client(dsn):
    app = create_app(dsn=dsn, webhook_secret=SECRET, platform_fee_bps=200)
    with TestClient(app) as c:
        c.provider = app.state.provider
        yield c


def _capture(client, shop_id: int, amount_paise: int) -> int:
    r = client.post(f"/shops/{shop_id}/payments", json={"amount_paise": amount_paise})
    assert r.status_code == 201
    body, sig = client.provider.event(r.json()["provider_ref"], "payment.captured")
    w = client.post("/webhooks/provider", content=body, headers={SIGNATURE_HEADER: sig})
    assert w.json() == {"outcome": "applied"}
    return r.json()["payment_id"]


def test_collect_then_check_balance(client):
    _capture(client, 5, 10_000)
    assert client.get("/shops/5/balance").json() == {"shop_id": 5, "balance_paise": 9_800}
    assert client.get("/health/invariants").json()["ok"] is True


def test_forged_webhook_gets_401(client):
    r = client.post("/shops/5/payments", json={"amount_paise": 10_000})
    body, _ = client.provider.event(r.json()["provider_ref"], "payment.captured")
    w = client.post("/webhooks/provider", content=body, headers={SIGNATURE_HEADER: "nope"})
    assert w.status_code == 401


def test_payout_retries_with_same_key_pay_once(client):
    _capture(client, 5, 10_000)
    payload = {"shop_id": 5, "supplier_id": 11, "amount_paise": 4_000}
    headers = {"Idempotency-Key": "payout-2026-11-19-0001"}

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post("/payouts", json=payload, headers=headers),
                                  range(5)))

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["payout_id"] for r in responses}) == 1
    assert {r.json()["status"] for r in responses} == {"processing"}
    assert len(client.provider.payouts) == 1
    assert client.get("/shops/5/balance").json()["balance_paise"] == 5_800


def test_reused_key_with_different_amount_gets_422(client):
    _capture(client, 5, 10_000)
    headers = {"Idempotency-Key": "payout-2026-11-19-0002"}
    client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 1_000}, headers=headers)
    r = client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 2_000}, headers=headers)
    assert r.status_code == 422


def test_payout_without_key_is_refused(client):
    r = client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 1_000})
    assert r.status_code == 422


def test_overdraw_gets_402(client):
    _capture(client, 5, 1_000)
    r = client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 5_000},
                    headers={"Idempotency-Key": "payout-too-big-01"})
    assert r.status_code == 402


def test_refund_flow(client):
    pid = _capture(client, 5, 10_000)
    assert client.post(f"/payments/{pid}/refund").status_code == 200
    assert client.get("/shops/5/balance").json()["balance_paise"] == 0
    statuses = [h["to_status"] for h in client.get(f"/payments/{pid}").json()["history"]]
    assert statuses == ["pending", "captured", "refunded"]
    assert client.post(f"/payments/{pid}/refund").json()["changed"] is False


def test_payout_timeout_then_webhook_then_reversal(client):
    _capture(client, 5, 10_000)
    client.provider.lose_next_response = True
    r = client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 3_000},
                    headers={"Idempotency-Key": "payout-timeout-01"})
    assert r.json()["status"] == "queued"                  # we never heard back
    ref = next(iter(client.provider.payouts))
    body, sig = client.provider.event(ref, "payout.processed")
    client.post("/webhooks/provider", content=body, headers={SIGNATURE_HEADER: sig})
    assert client.get(f"/payouts/{r.json()['payout_id']}").json()["status"] == "processed"
    body, sig = client.provider.event(ref, "payout.reversed")
    client.post("/webhooks/provider", content=body, headers={SIGNATURE_HEADER: sig})
    assert client.get("/shops/5/balance").json()["balance_paise"] == 9_800
    assert client.get("/health/invariants").json()["ok"] is True


def test_sync_never_sends_money(client):
    _capture(client, 5, 10_000)
    r = client.post("/payouts", json={"shop_id": 5, "supplier_id": 11, "amount_paise": 1_000},
                    headers={"Idempotency-Key": "payout-sync-0001"})
    for _ in range(3):
        client.post(f"/payouts/{r.json()['payout_id']}/sync")
    assert len(client.provider.payouts) == 1
