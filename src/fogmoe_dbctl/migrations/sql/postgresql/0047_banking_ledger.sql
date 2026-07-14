-- migrate:up

-- The bank is the monetary source of truth.  identity.users.coins and
-- coins_paid are intentionally imported once below; subsequent monetary code
-- must write append-only postings rather than mutate those legacy columns.
CREATE SCHEMA IF NOT EXISTS bank;

-- The bank ledger is BIGINT-based; widen legacy projection columns before a
-- posting trigger mirrors the bank read model into them.
ALTER TABLE identity.users
  ALTER COLUMN coins TYPE BIGINT,
  ALTER COLUMN coins_paid TYPE BIGINT;

CREATE TABLE bank.accounts (
  account_key VARCHAR(240) PRIMARY KEY
    CHECK (account_key ~ '^[a-z][a-z0-9:_-]{0,239}$'),
  account_scope TEXT NOT NULL
    CHECK (account_scope IN ('user', 'group', 'system')),
  owner_id BIGINT NULL,
  token_bucket TEXT NULL
    CHECK (token_bucket IS NULL OR token_bucket IN ('free', 'paid')),
  system_kind TEXT NULL
    CHECK (system_kind IS NULL OR system_kind IN (
      'issuance', 'burn', 'group_treasury', 'activity_pot'
    )),
  allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT bank_accounts_shape_ck CHECK (
    (
      account_scope = 'user'
      AND owner_id IS NOT NULL AND owner_id > 0
      AND token_bucket IS NOT NULL AND system_kind IS NULL
      AND NOT allow_negative
    )
    OR (
      account_scope = 'group'
      AND owner_id IS NOT NULL AND owner_id <> 0
      AND token_bucket IS NULL AND system_kind = 'group_treasury'
      AND NOT allow_negative
    )
    OR (
      account_scope = 'system'
      AND owner_id IS NULL AND token_bucket IS NULL AND system_kind IS NOT NULL
      AND system_kind IN ('issuance', 'burn', 'activity_pot')
      AND allow_negative = (system_kind = 'issuance')
    )
  )
);

CREATE UNIQUE INDEX bank_accounts_user_identity_uq
  ON bank.accounts (owner_id, token_bucket)
  WHERE account_scope = 'user';

CREATE UNIQUE INDEX bank_accounts_group_identity_uq
  ON bank.accounts (owner_id)
  WHERE account_scope = 'group';

CREATE UNIQUE INDEX bank_accounts_system_identity_uq
  ON bank.accounts (system_kind)
  WHERE account_scope = 'system';

CREATE TABLE bank.account_balances (
  account_key VARCHAR(240) PRIMARY KEY
    REFERENCES bank.accounts(account_key) ON DELETE RESTRICT,
  balance BIGINT NOT NULL DEFAULT 0,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bank.ledger_entries (
  entry_id UUID PRIMARY KEY,
  idempotency_key VARCHAR(200) NOT NULL UNIQUE
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  reason TEXT NOT NULL CHECK (reason IN (
    'bank_issuance', 'migration_opening', 'bank_burn', 'user_transfer',
    'group_contribution', 'activity_stake', 'activity_payout'
  )),
  actor_id BIGINT NULL REFERENCES identity.users(id) ON DELETE RESTRICT,
  metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    CHECK (jsonb_typeof(metadata) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX bank_ledger_entries_actor_created_idx
  ON bank.ledger_entries (actor_id, created_at DESC, entry_id DESC)
  WHERE actor_id IS NOT NULL;

CREATE INDEX bank_ledger_entries_reason_created_idx
  ON bank.ledger_entries (reason, created_at DESC, entry_id DESC);

CREATE TABLE bank.ledger_postings (
  entry_id UUID NOT NULL
    REFERENCES bank.ledger_entries(entry_id) ON DELETE RESTRICT,
  line_no SMALLINT NOT NULL CHECK (line_no > 0),
  account_key VARCHAR(240) NOT NULL
    REFERENCES bank.accounts(account_key) ON DELETE RESTRICT,
  delta BIGINT NOT NULL CHECK (delta <> 0),
  PRIMARY KEY (entry_id, line_no),
  CONSTRAINT bank_ledger_postings_account_once_uq UNIQUE (entry_id, account_key)
);

CREATE INDEX bank_ledger_postings_account_entry_idx
  ON bank.ledger_postings (account_key, entry_id, line_no);

CREATE FUNCTION bank.assert_ledger_entry_balanced()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  affected_entry_id UUID := COALESCE(NEW.entry_id, OLD.entry_id);
  posting_count INTEGER;
  total_delta NUMERIC;
BEGIN
  SELECT count(*), COALESCE(sum(delta), 0)
    INTO posting_count, total_delta
  FROM bank.ledger_postings
  WHERE entry_id = affected_entry_id;

  IF posting_count < 2 OR total_delta <> 0 THEN
    RAISE EXCEPTION 'bank ledger entry % is not balanced', affected_entry_id
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER bank_ledger_entries_balanced_ct
AFTER INSERT OR UPDATE OR DELETE ON bank.ledger_postings
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION bank.assert_ledger_entry_balanced();

CREATE FUNCTION bank.assert_nonnegative_account_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  can_be_negative BOOLEAN;
BEGIN
  SELECT allow_negative INTO can_be_negative
  FROM bank.accounts
  WHERE account_key = NEW.account_key;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'bank account % is missing', NEW.account_key
      USING ERRCODE = '23503';
  END IF;
  IF NEW.balance < 0 AND NOT can_be_negative THEN
    RAISE EXCEPTION 'bank account % cannot become negative', NEW.account_key
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER bank_account_balances_nonnegative_tr
BEFORE INSERT OR UPDATE OF balance ON bank.account_balances
FOR EACH ROW EXECUTE FUNCTION bank.assert_nonnegative_account_balance();

CREATE FUNCTION bank.apply_ledger_posting_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  affected_scope TEXT;
  affected_owner_id BIGINT;
  affected_bucket TEXT;
BEGIN
  INSERT INTO bank.account_balances (
    account_key, balance, version, updated_at
  ) VALUES (
    NEW.account_key, NEW.delta, 0, CURRENT_TIMESTAMP
  )
  ON CONFLICT (account_key) DO UPDATE
  SET balance = bank.account_balances.balance + EXCLUDED.balance,
      version = bank.account_balances.version + 1,
      updated_at = CURRENT_TIMESTAMP;

  -- During the one-way migration, legacy columns are a read projection only.
  -- This keeps not-yet-migrated feature readers coherent while no application
  -- code is permitted to treat them as an authoritative monetary write path.
  SELECT account_scope, owner_id, token_bucket
    INTO affected_scope, affected_owner_id, affected_bucket
  FROM bank.accounts
  WHERE account_key = NEW.account_key;
  IF affected_scope = 'user' AND affected_owner_id IS NOT NULL THEN
    IF affected_bucket = 'free' THEN
      UPDATE identity.users AS users
      SET coins = balances.balance
      FROM bank.account_balances AS balances
      WHERE balances.account_key = NEW.account_key
        AND users.id = affected_owner_id;
    ELSIF affected_bucket = 'paid' THEN
      UPDATE identity.users AS users
      SET coins_paid = balances.balance
      FROM bank.account_balances AS balances
      WHERE balances.account_key = NEW.account_key
        AND users.id = affected_owner_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER bank_ledger_postings_apply_balance_tr
AFTER INSERT ON bank.ledger_postings
FOR EACH ROW EXECUTE FUNCTION bank.apply_ledger_posting_balance();

CREATE FUNCTION bank.forbid_ledger_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'bank ledger is append-only; use a compensating entry instead'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER bank_ledger_entries_append_only_tr
BEFORE UPDATE OR DELETE ON bank.ledger_entries
FOR EACH ROW EXECUTE FUNCTION bank.forbid_ledger_mutation();

CREATE TRIGGER bank_ledger_postings_append_only_tr
BEFORE UPDATE OR DELETE ON bank.ledger_postings
FOR EACH ROW EXECUTE FUNCTION bank.forbid_ledger_mutation();

CREATE TABLE bank.token_requests (
  request_id UUID PRIMARY KEY,
  requester_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  requested_amount BIGINT NOT NULL CHECK (requested_amount > 0),
  requested_bucket TEXT NOT NULL DEFAULT 'free'
    CHECK (requested_bucket = 'free'),
  purpose TEXT NOT NULL CHECK (char_length(btrim(purpose)) BETWEEN 1 AND 500),
  status TEXT NOT NULL CHECK (status IN (
    'pending', 'approved', 'rejected', 'cancelled'
  )),
  requested_at TIMESTAMPTZ NOT NULL,
  reviewed_at TIMESTAMPTZ NULL,
  reviewer_id BIGINT NULL REFERENCES identity.users(id) ON DELETE RESTRICT,
  review_note TEXT NULL CHECK (
    review_note IS NULL OR char_length(btrim(review_note)) <= 500
  ),
  ledger_entry_id UUID NULL UNIQUE
    REFERENCES bank.ledger_entries(entry_id) ON DELETE RESTRICT,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT bank_token_requests_terminal_shape_ck CHECK (
    (
      status = 'pending'
      AND reviewed_at IS NULL AND reviewer_id IS NULL AND ledger_entry_id IS NULL
    )
    OR (
      status = 'approved'
      AND reviewed_at IS NOT NULL AND reviewer_id IS NOT NULL
      AND reviewer_id <> requester_id AND ledger_entry_id IS NOT NULL
    )
    OR (
      status = 'rejected'
      AND reviewed_at IS NOT NULL AND reviewer_id IS NOT NULL
      AND reviewer_id <> requester_id AND ledger_entry_id IS NULL
    )
    OR (
      status = 'cancelled'
      AND reviewed_at IS NOT NULL AND reviewer_id = requester_id
      AND ledger_entry_id IS NULL
    )
  ),
  CONSTRAINT bank_token_requests_time_order_ck CHECK (
    reviewed_at IS NULL OR reviewed_at >= requested_at
  )
);

CREATE INDEX bank_token_requests_pending_idx
  ON bank.token_requests (requested_at, request_id)
  WHERE status = 'pending';

CREATE INDEX bank_token_requests_requester_created_idx
  ON bank.token_requests (requester_id, requested_at DESC, request_id DESC);

CREATE TABLE bank.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  operation_kind VARCHAR(80) NOT NULL
    CHECK (char_length(btrim(operation_kind)) BETWEEN 1 AND 80),
  actor_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX bank_operation_receipts_actor_created_idx
  ON bank.operation_receipts (actor_id, created_at DESC);

-- System accounts are explicit so every issue, burn and escrow movement has a
-- visible counterparty.  Only issuance may be negative: it represents the
-- bank's authorised minting capacity rather than a user-owned balance.
INSERT INTO bank.accounts (
  account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
) VALUES
  ('system:issuance', 'system', NULL, NULL, 'issuance', TRUE),
  ('system:burn', 'system', NULL, NULL, 'burn', FALSE),
  ('system:activity_pot', 'system', NULL, NULL, 'activity_pot', FALSE);

-- Create both wallet pockets before importing balances, including zero
-- balances, so account overview is total and does not rely on nullable rows.
INSERT INTO bank.accounts (
  account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
)
SELECT 'user:' || users.id::TEXT || ':free', 'user', users.id, 'free', NULL, FALSE
FROM identity.users AS users
WHERE users.id > 0
UNION ALL
SELECT 'user:' || users.id::TEXT || ':paid', 'user', users.id, 'paid', NULL, FALSE
FROM identity.users AS users
WHERE users.id > 0;

INSERT INTO bank.account_balances (account_key, balance, version, updated_at)
SELECT account_key, 0, 0, CURRENT_TIMESTAMP
FROM bank.accounts;

-- A historical balance is preserved as an opening double-entry transaction.
-- The trigger above builds the cache projection from these postings, allowing
-- the migration to be reconciled without treating old integer columns as a
-- second source of truth.
WITH opening_balances AS (
  SELECT
    users.id AS user_id,
    'free'::TEXT AS token_bucket,
    users.coins::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':free' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins > 0
  UNION ALL
  SELECT
    users.id AS user_id,
    'paid'::TEXT AS token_bucket,
    users.coins_paid::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':paid' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins_paid > 0
)
INSERT INTO bank.ledger_entries (
  entry_id, idempotency_key, reason, actor_id, metadata, created_at
)
SELECT
  (
    substr(md5(opening.idempotency_key), 1, 8) || '-' ||
    substr(md5(opening.idempotency_key), 9, 4) || '-' ||
    substr(md5(opening.idempotency_key), 13, 4) || '-' ||
    substr(md5(opening.idempotency_key), 17, 4) || '-' ||
    substr(md5(opening.idempotency_key), 21, 12)
  )::UUID,
  opening.idempotency_key,
  'migration_opening',
  NULL,
  jsonb_build_object(
    'migration', '0047_banking_ledger',
    'legacy_bucket', opening.token_bucket
  ),
  CURRENT_TIMESTAMP
FROM opening_balances AS opening;

INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT entry.entry_id, 1, 'system:issuance', -opening.amount
FROM bank.ledger_entries AS entry
JOIN (
  SELECT
    users.id AS user_id,
    'free'::TEXT AS token_bucket,
    users.coins::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':free' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins > 0
  UNION ALL
  SELECT
    users.id AS user_id,
    'paid'::TEXT AS token_bucket,
    users.coins_paid::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':paid' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins_paid > 0
) AS opening ON opening.idempotency_key = entry.idempotency_key
WHERE entry.reason = 'migration_opening';

INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT
  entry.entry_id,
  2,
  'user:' || opening.user_id::TEXT || ':' || opening.token_bucket,
  opening.amount
FROM bank.ledger_entries AS entry
JOIN (
  SELECT
    users.id AS user_id,
    'free'::TEXT AS token_bucket,
    users.coins::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':free' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins > 0
  UNION ALL
  SELECT
    users.id AS user_id,
    'paid'::TEXT AS token_bucket,
    users.coins_paid::BIGINT AS amount,
    'migration:0047:opening:' || users.id::TEXT || ':paid' AS idempotency_key
  FROM identity.users AS users
  WHERE users.id > 0 AND users.coins_paid > 0
) AS opening ON opening.idempotency_key = entry.idempotency_key
WHERE entry.reason = 'migration_opening';

-- migrate:down

DROP SCHEMA IF EXISTS bank CASCADE;

ALTER TABLE identity.users
  ALTER COLUMN coins TYPE INT,
  ALTER COLUMN coins_paid TYPE INT;
