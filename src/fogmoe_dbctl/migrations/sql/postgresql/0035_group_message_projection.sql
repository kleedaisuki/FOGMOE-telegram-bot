-- migrate:up

-- Evolve the legacy append-only table in place so every historical row and
-- identity value survives.  Non-canonical duplicates remain queryable in the
-- same relation; only the newest legacy row participates in the canonical key.
ALTER TABLE conversation.chat_records_group
  RENAME TO group_message_projection;

ALTER TABLE conversation.group_message_projection
  RENAME CONSTRAINT chat_records_group_pkey TO group_message_projection_pkey;

ALTER SEQUENCE conversation.chat_records_group_id_seq
  RENAME TO group_message_projection_id_seq;

ALTER INDEX conversation.idx_group_created
  RENAME TO idx_group_message_projection_created;

ALTER INDEX conversation.idx_group_message
  RENAME TO idx_group_message_projection_message;

ALTER TABLE conversation.group_message_projection
  RENAME CONSTRAINT chat_records_group_message_type_check
  TO group_message_projection_message_type_check;

-- The legacy writer normalized aware UTC to a naive UTC timestamp.  Restore
-- that lost type information without changing the represented instant.
ALTER TABLE conversation.group_message_projection
  ALTER COLUMN created_at TYPE TIMESTAMPTZ
    USING created_at AT TIME ZONE 'UTC',
  ADD COLUMN source_update_id BIGINT,
  ADD COLUMN content_encoding TEXT NOT NULL DEFAULT 'plain',
  ADD COLUMN is_edited BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN is_canonical BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN updated_at TIMESTAMPTZ;

-- Every legacy non-text value came from the old base64 writer.  Keep its bytes
-- unchanged and make the encoding explicit so new rows can remain plain text.
UPDATE conversation.group_message_projection
SET content_encoding = 'base64'
WHERE message_type <> 'text';

UPDATE conversation.group_message_projection
SET updated_at = created_at;

WITH ranked AS (
  SELECT
    id,
    row_number() OVER (
      PARTITION BY group_id, message_id
      ORDER BY created_at DESC, id DESC
    ) AS ordinal
  FROM conversation.group_message_projection
)
UPDATE conversation.group_message_projection AS projection
SET is_canonical = (ranked.ordinal = 1)
FROM ranked
WHERE projection.id = ranked.id;

ALTER TABLE conversation.group_message_projection
  ALTER COLUMN updated_at SET NOT NULL,
  ADD CONSTRAINT group_message_projection_source_update_ck CHECK (
    source_update_id IS NULL OR source_update_id >= 0
  ),
  ADD CONSTRAINT group_message_projection_encoding_ck CHECK (
    content_encoding IN ('plain', 'base64')
  ),
  ADD CONSTRAINT group_message_projection_time_ck CHECK (
    updated_at >= created_at
  );

CREATE UNIQUE INDEX uq_group_message_projection_canonical
  ON conversation.group_message_projection (group_id, message_id)
  WHERE is_canonical;

CREATE INDEX idx_group_message_projection_recent
  ON conversation.group_message_projection (group_id, message_id DESC, id DESC)
  WHERE is_canonical;

-- migrate:down

DROP INDEX conversation.idx_group_message_projection_recent;
DROP INDEX conversation.uq_group_message_projection_canonical;

ALTER TABLE conversation.group_message_projection
  DROP CONSTRAINT group_message_projection_time_ck,
  DROP CONSTRAINT group_message_projection_encoding_ck,
  DROP CONSTRAINT group_message_projection_source_update_ck;

-- Re-establish the legacy non-text storage contract for rows written after
-- 0035.  Existing base64 rows are left byte-for-byte unchanged.
UPDATE conversation.group_message_projection
SET
  content = encode(convert_to(COALESCE(content, ''), 'UTF8'), 'base64'),
  content_encoding = 'base64'
WHERE message_type <> 'text'
  AND content_encoding = 'plain';

ALTER TABLE conversation.group_message_projection
  DROP COLUMN updated_at,
  DROP COLUMN is_canonical,
  DROP COLUMN is_edited,
  DROP COLUMN content_encoding,
  DROP COLUMN source_update_id,
  ALTER COLUMN created_at TYPE TIMESTAMP
    USING created_at AT TIME ZONE 'UTC';

ALTER TABLE conversation.group_message_projection
  RENAME CONSTRAINT group_message_projection_message_type_check
  TO chat_records_group_message_type_check;

ALTER INDEX conversation.idx_group_message_projection_created
  RENAME TO idx_group_created;

ALTER INDEX conversation.idx_group_message_projection_message
  RENAME TO idx_group_message;

ALTER SEQUENCE conversation.group_message_projection_id_seq
  RENAME TO chat_records_group_id_seq;

ALTER TABLE conversation.group_message_projection
  RENAME CONSTRAINT group_message_projection_pkey TO chat_records_group_pkey;

ALTER TABLE conversation.group_message_projection
  RENAME TO chat_records_group;
