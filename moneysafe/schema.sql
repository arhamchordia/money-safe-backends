-- MoneySafe schema for PostgreSQL 16. Amounts are always BIGINT paise. Never float.
-- Safe to run more than once.

-- Shared trigger function: some tables are append-only.
CREATE OR REPLACE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: post a reversal instead', TG_TABLE_NAME;
END $$;

-- Part 1: the double-entry ledger ------------------------------------------

CREATE TABLE IF NOT EXISTS accounts (
    id    TEXT PRIMARY KEY,                     -- e.g. 'shop:42:wallet'
    kind  TEXT NOT NULL CHECK (kind IN ('asset', 'liability', 'revenue', 'expense')),
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memo       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Positive amount = debit, negative amount = credit.
CREATE TABLE IF NOT EXISTS postings (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     BIGINT NOT NULL REFERENCES journal_entries(id),
    account_id   TEXT   NOT NULL REFERENCES accounts(id),
    amount_paise BIGINT NOT NULL CHECK (amount_paise <> 0)
);
CREATE INDEX IF NOT EXISTS idx_postings_account ON postings(account_id);
CREATE INDEX IF NOT EXISTS idx_postings_entry ON postings(entry_id);

-- The database itself refuses an entry whose postings do not sum to zero.
-- DEFERRED: checked at COMMIT, after all of an entry's postings are in.
CREATE OR REPLACE FUNCTION check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    total BIGINT;
BEGIN
    SELECT COALESCE(SUM(amount_paise), 0) INTO total FROM postings WHERE entry_id = NEW.entry_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'journal entry % does not balance: postings sum to %', NEW.entry_id, total;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS postings_balanced ON postings;
CREATE CONSTRAINT TRIGGER postings_balanced AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();

-- The ledger is append-only. Mistakes are fixed with a reversing entry.
CREATE OR REPLACE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE OR REPLACE TRIGGER entries_append_only BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- System accounts every MoneySafe install needs.
INSERT INTO accounts (id, kind, name) VALUES
    ('provider:clearing', 'asset',     'Captured money the payment provider has not yet settled to us'),
    ('bank:main',         'asset',     'MoneySafe bank account'),
    ('payouts:in_flight', 'liability', 'Shop money held for payouts not yet sent to the bank'),
    ('moneysafe:fees',    'revenue',   'Platform fees MoneySafe charges shops (not MDR)'),
    ('provider:fees',     'expense',   'Fees and taxes the payment provider deducts at settlement')
ON CONFLICT (id) DO NOTHING;

-- Part 2: the race-condition demo -------------------------------------------
-- A deliberately simple "balance column" table, used to show lost updates.

CREATE TABLE IF NOT EXISTS wallets (
    id            BIGINT PRIMARY KEY,
    balance_paise BIGINT NOT NULL
);

-- Part 3: idempotency -------------------------------------------------------

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response     JSONB,                         -- filled in when the work commits
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A payout to a shop's supplier. Lifecycle modelled on real payout APIs
-- (RazorpayX: queued, processing, processed, reversed, failed).
CREATE TABLE IF NOT EXISTS payouts (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shop_id            BIGINT NOT NULL,
    supplier_id        BIGINT NOT NULL,
    amount_paise       BIGINT NOT NULL CHECK (amount_paise > 0),
    status             TEXT   NOT NULL DEFAULT 'queued' CHECK (status IN
                         ('queued', 'processing', 'processed', 'reversed', 'failed')),
    provider_key       TEXT   NOT NULL UNIQUE,   -- idempotency key we send the provider
    provider_ref       TEXT   UNIQUE,            -- the provider's payout id, once known
    first_submitted_at TIMESTAMPTZ,              -- when we first called the provider
    version            INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payout_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payout_id   BIGINT NOT NULL REFERENCES payouts(id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    source      TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE TRIGGER payout_events_append_only BEFORE UPDATE OR DELETE ON payout_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- Part 4: payments, webhooks and the outbox ---------------------------------

CREATE TABLE IF NOT EXISTS payments (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shop_id      BIGINT NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
    fee_paise    BIGINT NOT NULL CHECK (fee_paise >= 0),
    status       TEXT   NOT NULL CHECK (status IN
                   ('created', 'pending', 'captured', 'failed', 'refunded')),
    provider_ref TEXT UNIQUE,
    version      INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id  BIGINT NOT NULL REFERENCES payments(id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    source      TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE TRIGGER payment_events_append_only BEFORE UPDATE OR DELETE ON payment_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- Every webhook we have seen, so duplicates are recognised.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    payload     JSONB NOT NULL,
    outcome     TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Messages to send after a commit (SMS, notifications, alerts). Written in the
-- same transaction as the business change, delivered later by workers.
CREATE TABLE IF NOT EXISTS outbox (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic      TEXT NOT NULL,
    payload    JSONB NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dead')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(id) WHERE status = 'pending';

-- Part 5: reconciliation and settlement -------------------------------------

-- One row per provider settlement: a single bank credit (with a UTR) that
-- covers many captured payments, net of the provider's fees and taxes.
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    utr           TEXT NOT NULL UNIQUE,
    gross_paise   BIGINT NOT NULL,
    fees_paise    BIGINT NOT NULL,
    net_paise     BIGINT NOT NULL,
    entry_id      BIGINT NOT NULL REFERENCES journal_entries(id),
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which payments each settlement covered. A payment can be settled only once.
CREATE TABLE IF NOT EXISTS settlement_items (
    provider_ref  TEXT PRIMARY KEY REFERENCES payments(provider_ref),
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id)
);
