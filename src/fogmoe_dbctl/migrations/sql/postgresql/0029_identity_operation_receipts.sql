-- migrate:up

-- Identity commands return the snapshot committed by their first execution.
-- This prevents a crash between the account transaction and response-outbox
-- insertion from rendering a different balance or personal-info response later.
CREATE TABLE identity.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) > 0),
  operation_kind VARCHAR(80) NOT NULL
    CHECK (char_length(btrim(operation_kind)) > 0),
  user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX identity_operation_receipts_user_created_idx
  ON identity.operation_receipts (user_id, created_at, idempotency_key);

-- migrate:down

DROP TABLE identity.operation_receipts;
