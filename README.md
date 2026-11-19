# Money-Safe Backends in Python

MoneySafe is a small payments backend for kirana shops, built to teach the parts of backend engineering where a bug costs real money: ledgers, concurrency, idempotency, payouts, webhooks and reconciliation.

It uses only Python and SQLite, with an in-process fake payment provider, so it runs anywhere with no network access.

## Quick start

```bash
pip install -e ".[dev]"
pytest                                   # the full test suite
python scripts/double_charge_demo.py     # the race-condition demo
uvicorn moneysafe.api:create_app --factory --reload   # the HTTP API, docs at /docs
```

## Layout

| Part | Module | Topic |
| --- | --- | --- |
| 1 | `money.py`, `ledger.py` | Paise instead of floats, double-entry ledger, reversals |
| 2 | `wallet.py` | Race conditions and the double charge |
| 3 | `idempotency.py`, `payouts.py` | Idempotency keys, holds, safe retries |
| 4 | `payments.py`, `payouts.py`, `webhooks.py`, `outbox.py`, `provider.py` | State machines, signed webhooks, the outbox, a fake provider |
| 5 | `recon.py` | Settlement reconciliation |
| 6 | `api.py` | FastAPI service |

Tests in `tests/` are split one file per part.
