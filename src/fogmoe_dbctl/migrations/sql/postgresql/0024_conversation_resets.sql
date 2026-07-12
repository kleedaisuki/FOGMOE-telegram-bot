-- migrate:up

-- A reset is an append-only visibility boundary, not destructive history
-- deletion.  The source identity makes an at-least-once command replay converge
-- on the same boundary, while through_sequence orders it with message appends.
CREATE TABLE conversation.conversation_history_resets (
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  source_kind VARCHAR(100) NOT NULL CHECK (
    source_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  source_key VARCHAR(255) NOT NULL CHECK (
    char_length(source_key) BETWEEN 1 AND 255
  ),
  source_update_id BIGINT UNIQUE
    REFERENCES conversation.inbound_updates(update_id),
  through_sequence BIGINT NOT NULL CHECK (through_sequence >= 0),
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source_kind, source_key),
  CONSTRAINT conversation_history_resets_telegram_source_ck CHECK (
    (source_kind = 'telegram.update') = (source_update_id IS NOT NULL)
    AND (
      source_kind <> 'telegram.update'
      OR source_key = source_update_id::TEXT
    )
  )
);

CREATE INDEX idx_conversation_history_resets_boundary
  ON conversation.conversation_history_resets (
    conversation_id,
    through_sequence DESC,
    created_at DESC
  );

-- migrate:down

DROP TABLE IF EXISTS conversation.conversation_history_resets;
