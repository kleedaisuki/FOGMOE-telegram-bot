-- migrate:up

-- Every Telegram write is replayable after a crash between domain commit and
-- inbox acknowledgement.  Receipts keep the first canonical result instead of
-- repeating an old bind, debit, or prediction after later state has changed.
CREATE TABLE crypto.operation_receipts (
  idempotency_key TEXT PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 512),
  operation_kind TEXT NOT NULL
    CHECK (char_length(btrim(operation_kind)) BETWEEN 1 AND 100),
  actor_id BIGINT NOT NULL,
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Give every legacy swap a stable identity before making it mandatory.
ALTER TABLE crypto.token_swap_requests
  ADD COLUMN idempotency_key TEXT;

UPDATE crypto.token_swap_requests
SET idempotency_key = 'legacy:swap:' || id::TEXT;

ALTER TABLE crypto.token_swap_requests
  ALTER COLUMN idempotency_key SET NOT NULL,
  ADD CONSTRAINT token_swap_request_idempotency_key_ck
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 512),
  ADD CONSTRAINT token_swap_request_idempotency_key_uk
    UNIQUE (idempotency_key);

-- Old process-local locking could admit several pending rows for one user.
-- Preserve every charged request, retain the newest row as the canonical
-- pending request (matching PostgreSQL's old ORDER BY request_time DESC,
-- including its default NULLS FIRST behavior), and put
-- older conflicts into an explicit manual-review queue.  Downgrade restores
-- their original pending status.
CREATE TABLE crypto.swap_request_conflicts (
  request_id INTEGER PRIMARY KEY
    REFERENCES crypto.token_swap_requests(id) ON DELETE RESTRICT,
  canonical_request_id INTEGER NOT NULL
    REFERENCES crypto.token_swap_requests(id) ON DELETE RESTRICT,
  reason TEXT NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (request_id <> canonical_request_id)
);

WITH ranked AS (
  SELECT
    id,
    first_value(id) OVER (
      PARTITION BY user_id
      ORDER BY request_time DESC NULLS FIRST, id DESC
    ) AS canonical_id,
    row_number() OVER (
      PARTITION BY user_id
      ORDER BY request_time DESC NULLS FIRST, id DESC
    ) AS position
  FROM crypto.token_swap_requests
  WHERE status = 'pending'
)
INSERT INTO crypto.swap_request_conflicts (
  request_id,
  canonical_request_id,
  reason
)
SELECT
  id,
  canonical_id,
  'legacy_duplicate_pending_requires_manual_review'
FROM ranked
WHERE position > 1;

UPDATE crypto.token_swap_requests AS request
SET status = 'manual_review'
FROM crypto.swap_request_conflicts AS conflict
WHERE request.id = conflict.request_id;

CREATE UNIQUE INDEX uq_token_swap_requests_one_pending_per_user
  ON crypto.token_swap_requests (user_id)
  WHERE status = 'pending';

-- A prediction must survive process restart with its original delivery target.
-- Outcome columns distinguish a committed settlement from a missing timer, while
-- legacy completed rows are intentionally allowed to retain an empty outcome.
ALTER TABLE crypto.user_btc_predictions
  ADD COLUMN request_key TEXT,
  ADD COLUMN chat_id BIGINT,
  ADD COLUMN end_price DECIMAL(20,8),
  ADD COLUMN is_correct BOOLEAN,
  ADD COLUMN reward INTEGER,
  ADD COLUMN settled_at TIMESTAMPTZ;

UPDATE crypto.user_btc_predictions
SET
  request_key = 'legacy:btc:' || user_id::TEXT || ':'
    || extract(epoch FROM start_time)::TEXT,
  chat_id = user_id;

ALTER TABLE crypto.user_btc_predictions
  ALTER COLUMN request_key SET NOT NULL,
  ALTER COLUMN chat_id SET NOT NULL,
  ADD CONSTRAINT user_btc_predictions_request_key_ck
    CHECK (char_length(btrim(request_key)) BETWEEN 1 AND 512),
  ADD CONSTRAINT user_btc_predictions_request_key_uk UNIQUE (request_key),
  ADD CONSTRAINT user_btc_predictions_reward_ck
    CHECK (reward IS NULL OR reward >= 0),
  ADD CONSTRAINT user_btc_predictions_outcome_shape_ck CHECK (
    (
      end_price IS NULL
      AND is_correct IS NULL
      AND reward IS NULL
      AND settled_at IS NULL
    )
    OR
    (
      end_price IS NOT NULL
      AND end_price > 0
      AND is_correct IS NOT NULL
      AND reward IS NOT NULL
      AND settled_at IS NOT NULL
    )
  ),
  ADD CONSTRAINT user_btc_predictions_pending_outcome_ck CHECK (
    is_completed
    OR (
      end_price IS NULL
      AND is_correct IS NULL
      AND reward IS NULL
      AND settled_at IS NULL
    )
  );

CREATE INDEX idx_user_btc_predictions_due
  ON crypto.user_btc_predictions (end_time, user_id)
  WHERE is_completed = FALSE;

-- migrate:down

DROP INDEX crypto.idx_user_btc_predictions_due;

ALTER TABLE crypto.user_btc_predictions
  DROP CONSTRAINT user_btc_predictions_pending_outcome_ck,
  DROP CONSTRAINT user_btc_predictions_outcome_shape_ck,
  DROP CONSTRAINT user_btc_predictions_reward_ck,
  DROP CONSTRAINT user_btc_predictions_request_key_uk,
  DROP CONSTRAINT user_btc_predictions_request_key_ck,
  DROP COLUMN settled_at,
  DROP COLUMN reward,
  DROP COLUMN is_correct,
  DROP COLUMN end_price,
  DROP COLUMN chat_id,
  DROP COLUMN request_key;

DROP INDEX crypto.uq_token_swap_requests_one_pending_per_user;

UPDATE crypto.token_swap_requests AS request
SET status = 'pending'
FROM crypto.swap_request_conflicts AS conflict
WHERE request.id = conflict.request_id;

DROP TABLE crypto.swap_request_conflicts;

ALTER TABLE crypto.token_swap_requests
  DROP CONSTRAINT token_swap_request_idempotency_key_uk,
  DROP CONSTRAINT token_swap_request_idempotency_key_ck,
  DROP COLUMN idempotency_key;

DROP TABLE crypto.operation_receipts;
