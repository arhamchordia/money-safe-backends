# Money-Safe Backends in Python

MoneySafe is a small payments backend for kirana shops, built to teach the parts of backend engineering where a bug costs real money: ledgers, concurrency, idempotency, payouts, webhooks and reconciliation.

It runs on Python 3.13 and PostgreSQL 16, with an in-process fake payment provider, so it needs no outside network.

## Quick start

```bash
pip install -e ".[dev]"

# Tests create a fresh copy of the database per test, so they need a role that can create databases
export MONEYSAFE_ADMIN_DSN="postgresql://postgres@localhost/postgres"
pytest

# The demo and the API use one database
export MONEYSAFE_DSN="postgresql://postgres@localhost/moneysafe"
python scripts/double_charge_demo.py                    # the race-condition demo
uvicorn moneysafe.api:create_app --factory --reload     # the HTTP API, docs at /docs
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
