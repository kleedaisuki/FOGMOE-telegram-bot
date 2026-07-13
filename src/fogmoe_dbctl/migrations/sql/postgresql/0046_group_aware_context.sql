-- migrate:up

-- A group timeline is multi-party data. Preserve provider speaker identity even when a
-- member has no local account, and keep Telegram forum topics as independent context pages.
ALTER TABLE conversation.group_message_projection
  ADD COLUMN message_thread_id BIGINT,
  ADD COLUMN sender_name TEXT,
  ADD COLUMN sender_username TEXT,
  ADD CONSTRAINT group_message_projection_thread_ck CHECK (
    message_thread_id IS NULL OR message_thread_id > 0
  ),
  ADD CONSTRAINT group_message_projection_sender_name_ck CHECK (
    sender_name IS NULL OR char_length(btrim(sender_name)) BETWEEN 1 AND 256
  ),
  ADD CONSTRAINT group_message_projection_sender_username_ck CHECK (
    sender_username IS NULL OR char_length(btrim(sender_username)) BETWEEN 1 AND 64
  );

-- The projection is rebuildable derived data. Recover topic/speaker attributes only from
-- the durable source Update; rows without source provenance cannot be safely assigned to
-- Telegram's main topic and are discarded instead of contaminating another topic.
WITH source_messages AS (
  SELECT
    inbound.update_id,
    COALESCE(
      inbound.payload -> 'message',
      inbound.payload -> 'edited_message'
    ) AS message
  FROM conversation.inbound_updates AS inbound
)
UPDATE conversation.group_message_projection AS projection
SET
  message_thread_id = NULLIF(source.message ->> 'message_thread_id', '')::BIGINT,
  sender_name = COALESCE(
    NULLIF(
      btrim(
        concat_ws(
          ' ',
          source.message -> 'from' ->> 'first_name',
          source.message -> 'from' ->> 'last_name'
        )
      ),
      ''
    ),
    projection.sender_name
  ),
  sender_username = COALESCE(
    NULLIF(btrim(source.message -> 'from' ->> 'username'), ''),
    projection.sender_username
  )
FROM source_messages AS source
WHERE projection.source_update_id = source.update_id
  AND source.message IS NOT NULL;

DELETE FROM conversation.group_message_projection AS projection
WHERE NOT EXISTS (
  SELECT 1
  FROM conversation.inbound_updates AS inbound
  WHERE inbound.update_id = projection.source_update_id
    AND COALESCE(
      inbound.payload -> 'message',
      inbound.payload -> 'edited_message'
    ) IS NOT NULL
);

CREATE INDEX idx_group_message_projection_topic_recent
  ON conversation.group_message_projection (
    group_id,
    message_thread_id,
    message_id DESC,
    id DESC
  )
  WHERE is_canonical;

-- migrate:down

DROP INDEX conversation.idx_group_message_projection_topic_recent;

ALTER TABLE conversation.group_message_projection
  DROP CONSTRAINT group_message_projection_sender_username_ck,
  DROP CONSTRAINT group_message_projection_sender_name_ck,
  DROP CONSTRAINT group_message_projection_thread_ck,
  DROP COLUMN sender_username,
  DROP COLUMN sender_name,
  DROP COLUMN message_thread_id;
