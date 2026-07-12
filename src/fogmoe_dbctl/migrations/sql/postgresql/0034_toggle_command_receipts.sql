-- migrate:up

-- Telegram inbox delivery is at-least-once.  A command receipt records the
-- first committed toggle result in the same transaction as the policy change,
-- so replay re-renders that result instead of reversing the switch again.
CREATE TABLE moderation.toggle_command_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) > 0),
  operation_kind VARCHAR(80) NOT NULL CHECK (
    operation_kind IN ('spam_control', 'member_verification')
  ),
  chat_id BIGINT NOT NULL,
  actor_id BIGINT NOT NULL,
  request_payload JSONB NOT NULL CHECK (
    jsonb_typeof(request_payload) = 'object'
  ),
  enabled BOOLEAN NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX toggle_command_receipts_chat_created_idx
  ON moderation.toggle_command_receipts
    (operation_kind, chat_id, created_at, idempotency_key);

-- migrate:down

DROP TABLE moderation.toggle_command_receipts;
