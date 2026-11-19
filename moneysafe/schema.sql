-- MoneySafe schema. Amounts are always INTEGER paise. Never REAL.

-- Part 1: the double-entry ledger ------------------------------------------

CREATE TABLE IF NOT EXISTS accounts (
    id    TEXT PRIMARY KEY,                     -- e.g. 'shop:42:wallet'
    kind  TEXT NOT NULL CHECK (kind IN ('asset', 'liability', 'revenue', 'expense')),
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memo       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Positive amount = debit, negative amount = credit.
-- Every journal entry's postings must sum to zero (enforced in ledger.py).
CREATE TABLE IF NOT EXISTS postings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     INTEGER NOT NULL REFERENCES journal_entries(id),
    account_id   TEXT    NOT NULL REFERENCES accounts(id),
    amount_paise INTEGER NOT NULL CHECK (amount_paise <> 0)
);
CREATE INDEX IF NOT EXISTS idx_postings_account ON postings(account_id);

-- The ledger is append-only. Mistakes are fixed with a reversing entry.
CREATE TRIGGER IF NOT EXISTS postings_no_update BEFORE UPDATE ON postings
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: post a reversal instead'); END;
CREATE TRIGGER IF NOT EXISTS postings_no_delete BEFORE DELETE ON postings
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: post a reversal instead'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: post a reversal instead'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: post a reversal instead'); END;

-- System accounts every MoneySafe install needs.
INSERT OR IGNORE INTO accounts (id, kind, name) VALUES
    ('provider:clearing', 'asset',     'Captured money the payment provider has not yet settled to us'),
    ('bank:main',         'asset',     'MoneySafe bank account'),
    ('payouts:in_flight', 'liability', 'Shop money held for payouts not yet sent to the bank'),
    ('moneysafe:fees',     'revenue',   'Platform fees MoneySafe charges shops (not MDR)'),
    ('provider:fees',     'expense',   'Fees and taxes the payment provider deducts at settlement');

-- Part 2: the race-condition demo -------------------------------------------
-- A deliberately simple "balance column" table, used to show lost updates.

CREATE TABLE IF NOT EXISTS wallets (
    id            INTEGER PRIMARY KEY,
    balance_paise INTEGER NOT NULL
);

-- Part 3: idempotency -------------------------------------------------------

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response     TEXT,                          -- JSON, filled in when the work commits
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A payout to a shop's supplier. Lifecycle modelled on real payout APIs
-- (RazorpayX: queued, processing, processed, reversed, failed).
CREATE TABLE IF NOT EXISTS payouts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id            INTEGER NOT NULL,
    supplier_id        INTEGER NOT NULL,
    amount_paise       INTEGER NOT NULL CHECK (amount_paise > 0),
    status             TEXT    NOT NULL DEFAULT 'queued' CHECK (status IN
                         ('queued', 'processing', 'processed', 'reversed', 'failed')),
    provider_key       TEXT    NOT NULL UNIQUE,   -- idempotency key we send the provider, derived from our id
    provider_ref       TEXT    UNIQUE,            -- the provider's payout id, once known
    first_submitted_at TEXT,                      -- when we first called the provider (key window starts)
    version            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS payout_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payout_id   INTEGER NOT NULL REFERENCES payouts(id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    source      TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TRIGGER IF NOT EXISTS payout_events_no_update BEFORE UPDATE ON payout_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS payout_events_no_delete BEFORE DELETE ON payout_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

-- Part 4: payments, webhooks and the outbox ---------------------------------

CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id      INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    fee_paise    INTEGER NOT NULL CHECK (fee_paise >= 0),
    status       TEXT    NOT NULL CHECK (status IN
                   ('created', 'pending', 'captured', 'failed', 'refunded')),
    provider_ref TEXT UNIQUE,
    version      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Audit trail of every status change. Append-only like the ledger.
CREATE TABLE IF NOT EXISTS payment_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id  INTEGER NOT NULL REFERENCES payments(id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    source      TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TRIGGER IF NOT EXISTS payment_events_no_update BEFORE UPDATE ON payment_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS payment_events_no_delete BEFORE DELETE ON payment_events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

-- Every webhook we have seen, so duplicates are recognised.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Messages to send after a commit (SMS, notifications). Written in the same
-- transaction as the business change, delivered later by a worker.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, id);

-- Part 5: reconciliation and settlement -------------------------------------

-- One row per provider settlement: a single bank credit (with a UTR) that
-- covers many captured payments, net of the provider's fees and taxes.
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    utr           TEXT NOT NULL UNIQUE,
    gross_paise   INTEGER NOT NULL,
    fees_paise    INTEGER NOT NULL,
    net_paise     INTEGER NOT NULL,
    entry_id      INTEGER NOT NULL REFERENCES journal_entries(id),
    posted_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Which payments each settlement covered. A payment can be settled only once.
CREATE TABLE IF NOT EXISTS settlement_items (
    provider_ref  TEXT PRIMARY KEY REFERENCES payments(provider_ref),
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id)
);
