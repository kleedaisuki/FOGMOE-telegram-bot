-- migrate:up

-- A model response is committed before any tool call it contains.  Replaying an
-- inference activity therefore consumes the same plan instead of asking the LLM
-- to invent a potentially different mutation after a crash.
CREATE TABLE assistant.tool_agent_steps (
  turn_id UUID NOT NULL
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE CASCADE,
  step_no SMALLINT NOT NULL CHECK (step_no BETWEEN 0 AND 32),
  request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  route_key VARCHAR(512) NOT NULL CHECK (char_length(btrim(route_key)) > 0),
  response JSONB NOT NULL CHECK (jsonb_typeof(response) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (turn_id, step_no)
);

-- Both read snapshots and mutations have a canonical result.  The composite key
-- is the terminal idempotency identity required by the architecture; request_hash
-- turns accidental ordinal reuse with different arguments into a conflict instead
-- of a second fact.
CREATE TABLE assistant.tool_effect_receipts (
  turn_id UUID NOT NULL
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE CASCADE,
  invocation_id VARCHAR(128) NOT NULL
    CHECK (char_length(btrim(invocation_id)) > 0),
  effect_kind VARCHAR(100) NOT NULL
    CHECK (char_length(btrim(effect_kind)) > 0),
  tool_name VARCHAR(100) NOT NULL
    CHECK (char_length(btrim(tool_name)) > 0),
  provider_call_id VARCHAR(512) NOT NULL
    CHECK (char_length(btrim(provider_call_id)) > 0),
  request_hash CHAR(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
  request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
  mutating BOOLEAN NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'succeeded', 'failed_final')),
  result JSONB,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY (turn_id, invocation_id, effect_kind),
  CHECK (
    (status = 'processing') =
    (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (status = 'succeeded') =
    (result IS NOT NULL AND completed_at IS NOT NULL)
  )
);

CREATE INDEX tool_effect_receipts_recovery_idx
  ON assistant.tool_effect_receipts (lease_expires_at, turn_id, invocation_id)
  WHERE status = 'processing';

CREATE INDEX tool_effect_receipts_kind_created_idx
  ON assistant.tool_effect_receipts (effect_kind, created_at, turn_id);

-- migrate:down

DROP TABLE assistant.tool_effect_receipts;
DROP TABLE assistant.tool_agent_steps;
