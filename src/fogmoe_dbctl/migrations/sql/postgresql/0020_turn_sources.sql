-- migrate:up

-- A Conversation Turn may originate from Telegram ingress or from another durable
-- source such as one occurrence of a recurring schedule.  The namespaced source
-- key is the idempotency identity; source_update_id remains an optional Telegram FK.
ALTER TABLE conversation.conversation_turns
  ALTER COLUMN source_update_id DROP NOT NULL;

ALTER TABLE conversation.conversation_turns
  ADD COLUMN source_kind VARCHAR(100),
  ADD COLUMN source_key VARCHAR(255);

UPDATE conversation.conversation_turns
SET source_kind = 'telegram.update',
    source_key = source_update_id::TEXT;

ALTER TABLE conversation.conversation_turns
  ALTER COLUMN source_kind SET NOT NULL,
  ALTER COLUMN source_key SET NOT NULL,
  ADD CONSTRAINT conversation_turns_source_kind_ck CHECK (
    source_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  ADD CONSTRAINT conversation_turns_source_key_ck CHECK (
    char_length(source_key) BETWEEN 1 AND 255
  ),
  ADD CONSTRAINT conversation_turns_source_identity_uq UNIQUE (
    source_kind,
    source_key
  ),
  ADD CONSTRAINT conversation_turns_telegram_source_ck CHECK (
    (source_kind = 'telegram.update') = (source_update_id IS NOT NULL)
    AND (
      source_kind <> 'telegram.update'
      OR source_key = source_update_id::TEXT
    )
  );

-- migrate:down

-- Pre-0020 cannot represent non-Telegram Turns.  Delete only those workflows and
-- their dependent rows before restoring the mandatory Telegram foreign key.
DELETE FROM conversation.outbound_messages
WHERE turn_id IN (
  SELECT turn_id FROM conversation.conversation_turns
  WHERE source_update_id IS NULL
);

DELETE FROM conversation.inference_activities
WHERE turn_id IN (
  SELECT turn_id FROM conversation.conversation_turns
  WHERE source_update_id IS NULL
);

DELETE FROM conversation.conversation_messages
WHERE turn_id IN (
  SELECT turn_id FROM conversation.conversation_turns
  WHERE source_update_id IS NULL
);

DELETE FROM conversation.conversation_turns
WHERE source_update_id IS NULL;

ALTER TABLE conversation.conversation_turns
  DROP CONSTRAINT conversation_turns_telegram_source_ck,
  DROP CONSTRAINT conversation_turns_source_identity_uq,
  DROP CONSTRAINT conversation_turns_source_key_ck,
  DROP CONSTRAINT conversation_turns_source_kind_ck,
  DROP COLUMN source_key,
  DROP COLUMN source_kind,
  ALTER COLUMN source_update_id SET NOT NULL;
