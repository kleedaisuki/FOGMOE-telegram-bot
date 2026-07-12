-- migrate:up

-- 0037 permits a Turn to own a delivery plan rather than exactly one outbox
-- effect.  Earlier runtime versions completed the Turn after the first effect,
-- which leaves later pending effects unclaimable.  Reopen only plans that
-- still contain active work; completed and permanently failed history remains
-- untouched.
UPDATE conversation.conversation_turns AS turn_row
SET state = 'waiting_delivery',
    version = turn_row.version + 1,
    next_retry_at = NULL,
    last_error = NULL,
    updated_at = CURRENT_TIMESTAMP,
    completed_at = NULL
WHERE turn_row.state IN ('delivered', 'delivery_retry_wait')
  AND EXISTS (
    SELECT 1
    FROM conversation.outbound_messages AS outbound
    WHERE outbound.turn_id = turn_row.turn_id
      AND outbound.status IN ('pending', 'processing', 'retry_wait')
  );

-- migrate:down

-- This is a data repair.  Restoring the inconsistent historical states would
-- reintroduce stranded effects, so downgrade is intentionally a no-op.
