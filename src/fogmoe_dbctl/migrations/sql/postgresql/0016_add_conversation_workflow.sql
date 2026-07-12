-- migrate:up

-- One database namespace serves one logical Telegram bot.  update_id is therefore
-- the durable idempotency key shared by every process instance consuming that bot.
CREATE TABLE conversation.inbound_updates (
  update_id BIGINT PRIMARY KEY CHECK (update_id >= 0),
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'processing', 'retry_wait', 'processed', 'failed_final')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  last_error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TIMESTAMPTZ,
  CONSTRAINT inbound_updates_claimable_time_ck CHECK (
    (status IN ('pending', 'retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT inbound_updates_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT inbound_updates_processed_at_ck CHECK (
    (status = 'processed') = (processed_at IS NOT NULL)
  )
);

CREATE INDEX idx_inbound_updates_ready
  ON conversation.inbound_updates (next_attempt_at, update_id)
  WHERE status IN ('pending', 'retry_wait');

CREATE INDEX idx_inbound_updates_expired_lease
  ON conversation.inbound_updates (lease_expires_at, update_id)
  WHERE status = 'processing';

CREATE INDEX idx_inbound_updates_conversation
  ON conversation.inbound_updates (conversation_id, received_at, update_id);

CREATE INDEX idx_inbound_updates_stream_head
  ON conversation.inbound_updates (conversation_id, update_id)
  WHERE status IN ('pending', 'processing', 'retry_wait');

CREATE TABLE conversation.conversation_turns (
  turn_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  source_update_id BIGINT NOT NULL UNIQUE REFERENCES conversation.inbound_updates(update_id),
  state TEXT NOT NULL DEFAULT 'received' CHECK (
    state IN (
      'received',
      'accepted',
      'waiting_inference',
      'inference_retry_wait',
      'inference_completed',
      'waiting_delivery',
      'delivery_retry_wait',
      'delivered',
      'cancelled',
      'failed_final'
    )
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  inference_attempts INTEGER NOT NULL DEFAULT 0 CHECK (inference_attempts >= 0),
  delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
  next_retry_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  CONSTRAINT conversation_turns_retry_time_ck CHECK (
    (state IN ('inference_retry_wait', 'delivery_retry_wait')) = (next_retry_at IS NOT NULL)
  ),
  CONSTRAINT conversation_turns_completed_at_ck CHECK (
    (state IN ('delivered', 'cancelled', 'failed_final')) = (completed_at IS NOT NULL)
  )
);

CREATE INDEX idx_conversation_turns_stream
  ON conversation.conversation_turns (conversation_id, created_at, turn_id);

CREATE INDEX idx_conversation_turns_retry
  ON conversation.conversation_turns (next_retry_at, turn_id)
  WHERE state IN ('inference_retry_wait', 'delivery_retry_wait');

CREATE INDEX idx_conversation_turns_active
  ON conversation.conversation_turns (conversation_id, updated_at, turn_id)
  WHERE state NOT IN ('delivered', 'cancelled', 'failed_final');

CREATE TABLE conversation.conversation_messages (
  message_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  turn_id UUID REFERENCES conversation.conversation_turns(turn_id),
  source_update_id BIGINT REFERENCES conversation.inbound_updates(update_id),
  role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
  content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
  idempotency_key VARCHAR(255) NOT NULL CHECK (char_length(idempotency_key) >= 1),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT conversation_messages_sequence_uq UNIQUE (conversation_id, sequence),
  CONSTRAINT conversation_messages_idempotency_uq UNIQUE (
    conversation_id,
    idempotency_key
  )
);

CREATE INDEX idx_conversation_messages_turn
  ON conversation.conversation_messages (turn_id, sequence)
  WHERE turn_id IS NOT NULL;

CREATE INDEX idx_conversation_messages_source_update
  ON conversation.conversation_messages (source_update_id)
  WHERE source_update_id IS NOT NULL;

CREATE TABLE conversation.outbound_messages (
  message_id UUID PRIMARY KEY,
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  turn_id UUID NOT NULL UNIQUE REFERENCES conversation.conversation_turns(turn_id),
  delivery_stream_id TEXT NOT NULL CHECK (
    char_length(delivery_stream_id) BETWEEN 1 AND 512
  ),
  stream_sequence BIGINT NOT NULL CHECK (stream_sequence >= 1),
  kind VARCHAR(100) NOT NULL CHECK (char_length(kind) >= 1),
  payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
  idempotency_key VARCHAR(255) NOT NULL CHECK (char_length(idempotency_key) >= 1),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'processing', 'retry_wait', 'delivered', 'failed_final', 'cancelled')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  external_message_id TEXT,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at TIMESTAMPTZ,
  CONSTRAINT outbound_messages_idempotency_uq UNIQUE (
    conversation_id,
    idempotency_key
  ),
  CONSTRAINT outbound_messages_stream_sequence_uq UNIQUE (
    delivery_stream_id,
    stream_sequence
  ),
  CONSTRAINT outbound_messages_claimable_time_ck CHECK (
    (status IN ('pending', 'retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT outbound_messages_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT outbound_messages_delivered_at_ck CHECK (
    (status = 'delivered') = (delivered_at IS NOT NULL)
  )
);

CREATE INDEX idx_outbound_messages_ready
  ON conversation.outbound_messages (
    next_attempt_at,
    delivery_stream_id,
    stream_sequence
  )
  WHERE status IN ('pending', 'retry_wait');

CREATE INDEX idx_outbound_messages_expired_lease
  ON conversation.outbound_messages (lease_expires_at, message_id)
  WHERE status = 'processing';

CREATE INDEX idx_outbound_messages_stream_head
  ON conversation.outbound_messages (delivery_stream_id, stream_sequence)
  WHERE status IN ('pending', 'processing', 'retry_wait');

-- migrate:down

DROP TABLE IF EXISTS conversation.outbound_messages;
DROP TABLE IF EXISTS conversation.conversation_messages;
DROP TABLE IF EXISTS conversation.conversation_turns;
DROP TABLE IF EXISTS conversation.inbound_updates;
