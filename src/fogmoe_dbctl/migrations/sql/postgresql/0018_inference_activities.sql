-- migrate:up

-- The request is an immutable, provider-neutral durable intent.  A Turn owns one
-- primary activity; worker ownership is transient and fenced by claim_token.
CREATE TABLE conversation.inference_activities (
  activity_id UUID PRIMARY KEY,
  turn_id UUID NOT NULL UNIQUE
    REFERENCES conversation.conversation_turns(turn_id),
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'processing', 'retry', 'completed', 'failed', 'cancelled')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  completion_token UUID,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  CONSTRAINT inference_activities_claimable_time_ck CHECK (
    (status IN ('pending', 'retry')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT inference_activities_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT inference_activities_claim_pair_ck CHECK (
    (claim_token IS NULL) = (lease_expires_at IS NULL)
  ),
  CONSTRAINT inference_activities_completed_at_ck CHECK (
    (status = 'completed') = (completed_at IS NOT NULL)
  ),
  CONSTRAINT inference_activities_completion_token_ck CHECK (
    (status = 'completed') = (completion_token IS NOT NULL)
  )
);

CREATE INDEX idx_inference_activities_ready
  ON conversation.inference_activities (next_attempt_at, activity_id)
  WHERE status IN ('pending', 'retry');

CREATE INDEX idx_inference_activities_expired_lease
  ON conversation.inference_activities (lease_expires_at, activity_id)
  WHERE status = 'processing';

CREATE UNIQUE INDEX uq_inference_activities_processing_conversation
  ON conversation.inference_activities (conversation_id)
  WHERE status = 'processing';

-- This index makes per-conversation diagnostics cheap without constraining
-- independent Turns to serialize behind one another.
CREATE INDEX idx_inference_activities_conversation
  ON conversation.inference_activities (conversation_id, created_at, activity_id);

-- migrate:down

DROP TABLE IF EXISTS conversation.inference_activities;
