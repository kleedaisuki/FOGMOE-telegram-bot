-- migrate:up

-- Turn-owned replies remain one primary outbound per Turn.  Rejection feedback and
-- similar control-plane notifications reuse the same ordered, fenced outbox without
-- manufacturing a Conversation Turn solely to satisfy a foreign key.
ALTER TABLE conversation.outbound_messages
  DROP CONSTRAINT outbound_messages_turn_id_key;

ALTER TABLE conversation.outbound_messages
  ALTER COLUMN turn_id DROP NOT NULL;

CREATE UNIQUE INDEX uq_outbound_messages_turn_id
  ON conversation.outbound_messages (turn_id)
  WHERE turn_id IS NOT NULL;

-- migrate:down

-- A pre-0019 schema cannot represent standalone effects.  Downgrade deliberately
-- discards only those rows before restoring the older mandatory-Turn invariant.
DELETE FROM conversation.outbound_messages
  WHERE turn_id IS NULL;

DROP INDEX conversation.uq_outbound_messages_turn_id;

ALTER TABLE conversation.outbound_messages
  ALTER COLUMN turn_id SET NOT NULL;

ALTER TABLE conversation.outbound_messages
  ADD CONSTRAINT outbound_messages_turn_id_key UNIQUE (turn_id);
