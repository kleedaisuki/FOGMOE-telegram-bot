-- migrate:up

DROP INDEX conversation.uq_outbound_messages_turn_id;

-- migrate:down

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM conversation.outbound_messages
    WHERE turn_id IS NOT NULL
    GROUP BY turn_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'cannot restore one-outbound-per-turn while delivery plans contain multiple effects';
  END IF;
END $$;

CREATE UNIQUE INDEX uq_outbound_messages_turn_id
  ON conversation.outbound_messages (turn_id)
  WHERE turn_id IS NOT NULL;
