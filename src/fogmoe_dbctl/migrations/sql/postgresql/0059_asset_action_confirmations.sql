-- migrate:up

-- An Agent may propose a change, but it can never directly mutate account assets.
-- The immutable proposal binds the action to one authenticated owner and private
-- Telegram chat.  A token-fenced execution lease permits crash recovery without
-- holding any database lock while the target bank operation runs.
CREATE TABLE assistant.asset_action_confirmations (
  confirmation_id UUID PRIMARY KEY,
  source_key VARCHAR(255) NOT NULL UNIQUE
    CHECK (char_length(btrim(source_key)) BETWEEN 1 AND 255),
  action_kind VARCHAR(100) NOT NULL
    CHECK (action_kind IN (
      'bank.review_token_request',
      'bank.issue_tokens',
      'bank.fund_activity_pot'
    )),
  owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  chat_id BIGINT NOT NULL CHECK (chat_id > 0),
  conversation_id TEXT NOT NULL
    CHECK (char_length(btrim(conversation_id)) BETWEEN 1 AND 512),
  delivery_stream_id TEXT NOT NULL
    CHECK (char_length(btrim(delivery_stream_id)) BETWEEN 1 AND 512),
  arguments JSONB NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'executing', 'executed', 'cancelled', 'expired')),
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  decision_update_id BIGINT UNIQUE CHECK (
    decision_update_id IS NULL OR decision_update_id >= 0
  ),
  decision_by_user_id BIGINT REFERENCES identity.users(id) ON DELETE RESTRICT,
  decision TEXT CHECK (decision IN ('approve', 'cancel')),
  decided_at TIMESTAMPTZ,
  execution_token UUID,
  execution_lease_expires_at TIMESTAMPTZ,
  execution_attempts INTEGER NOT NULL DEFAULT 0 CHECK (execution_attempts >= 0),
  result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
  executed_at TIMESTAMPTZ,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  CONSTRAINT asset_action_confirmations_expiry_ck CHECK (expires_at > created_at),
  CONSTRAINT asset_action_confirmations_private_chat_ck CHECK (chat_id = owner_user_id),
  CONSTRAINT asset_action_confirmations_updated_ck CHECK (updated_at >= created_at),
  CONSTRAINT asset_action_confirmations_decision_ck CHECK (
    (status IN ('executing', 'executed', 'cancelled')) = (
      decision_update_id IS NOT NULL
      AND decision_by_user_id IS NOT NULL
      AND decision IS NOT NULL
      AND decided_at IS NOT NULL
    )
  ),
  CONSTRAINT asset_action_confirmations_decision_shape_ck CHECK (
    num_nonnulls(
      decision_update_id,
      decision_by_user_id,
      decision,
      decided_at
    ) IN (0, 4)
  ),
  CONSTRAINT asset_action_confirmations_execution_lease_ck CHECK (
    (status = 'executing') = (
      execution_token IS NOT NULL
      AND execution_lease_expires_at IS NOT NULL
      AND execution_lease_expires_at > updated_at
    )
  ),
  CONSTRAINT asset_action_confirmations_execution_lease_shape_ck CHECK (
    num_nonnulls(execution_token, execution_lease_expires_at) IN (0, 2)
  ),
  CONSTRAINT asset_action_confirmations_execution_result_ck CHECK (
    (status = 'executed') = (
      result IS NOT NULL
      AND executed_at IS NOT NULL
      AND execution_attempts >= 1
    )
  ),
  CONSTRAINT asset_action_confirmations_execution_result_shape_ck CHECK (
    num_nonnulls(result, executed_at) IN (0, 2)
  )
);

CREATE INDEX asset_action_confirmations_pending_owner_idx
  ON assistant.asset_action_confirmations (owner_user_id, expires_at, confirmation_id)
  WHERE status = 'pending';

CREATE INDEX asset_action_confirmations_execution_recovery_idx
  ON assistant.asset_action_confirmations (
    execution_lease_expires_at,
    confirmation_id
  )
  WHERE status = 'executing';

-- migrate:down

DROP TABLE assistant.asset_action_confirmations;
