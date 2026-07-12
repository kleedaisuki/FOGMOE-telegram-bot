-- migrate:up

-- A single aggregate owns both the immutable segment input and the mutable
-- compaction lease.  This avoids parallel segment/job/summary state machines.
CREATE TABLE conversation.retention_segments (
  segment_id UUID PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('compaction', 'legacy_archive')),
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  owner_user_id BIGINT NOT NULL CHECK (owner_user_id > 0),
  epoch_floor_sequence BIGINT,
  from_sequence BIGINT,
  through_sequence BIGINT,
  anchor_turn_id UUID
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE RESTRICT,
  predecessor_segment_id UUID
    REFERENCES conversation.retention_segments(segment_id) ON DELETE RESTRICT,
  projection_version SMALLINT NOT NULL CHECK (projection_version >= 0),
  source_digest CHAR(64) NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
  source_snapshot JSON NOT NULL CHECK (json_typeof(source_snapshot) = 'array'),
  source_row_count INTEGER NOT NULL CHECK (source_row_count >= 0),
  source_token_count INTEGER NOT NULL CHECK (source_token_count >= 0),
  legacy_record_id BIGINT UNIQUE,
  legacy_summary_raw TEXT,
  status TEXT NOT NULL CHECK (
    status IN (
      'pending',
      'processing',
      'retry_wait',
      'completed',
      'failed_final',
      'cancelled'
    )
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  completion_token UUID,
  summary_text TEXT,
  summary_token_count INTEGER CHECK (
    summary_token_count BETWEEN 1 AND 2500
  ),
  summary_route_key VARCHAR(512),
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  CONSTRAINT retention_segments_kind_shape_ck CHECK (
    (
      kind = 'compaction'
      AND epoch_floor_sequence IS NOT NULL
      AND epoch_floor_sequence >= 0
      AND from_sequence IS NOT NULL
      AND from_sequence > epoch_floor_sequence
      AND through_sequence IS NOT NULL
      AND through_sequence >= from_sequence
      AND anchor_turn_id IS NOT NULL
      AND projection_version > 0
      AND source_row_count > 0
      AND json_array_length(source_snapshot) > 0
      AND legacy_record_id IS NULL
      AND legacy_summary_raw IS NULL
    ) OR (
      kind = 'legacy_archive'
      AND epoch_floor_sequence IS NULL
      AND from_sequence IS NULL
      AND through_sequence IS NULL
      AND anchor_turn_id IS NULL
      AND predecessor_segment_id IS NULL
      AND projection_version = 0
      AND source_row_count = json_array_length(source_snapshot)
      AND legacy_record_id IS NOT NULL
      AND status = 'completed'
    )
  ),
  CONSTRAINT retention_segments_claimable_time_ck CHECK (
    (status IN ('pending', 'retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT retention_segments_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT retention_segments_claim_pair_ck CHECK (
    (claim_token IS NULL) = (lease_expires_at IS NULL)
  ),
  CONSTRAINT retention_segments_terminal_time_ck CHECK (
    (status IN ('completed', 'failed_final', 'cancelled')) =
    (completed_at IS NOT NULL)
  ),
  CONSTRAINT retention_segments_completion_token_ck CHECK (
    (status = 'completed') = (completion_token IS NOT NULL)
  ),
  CONSTRAINT retention_segments_summary_shape_ck CHECK (
    (summary_text IS NULL AND summary_token_count IS NULL AND summary_route_key IS NULL)
    OR (
      summary_text IS NOT NULL
      AND char_length(btrim(summary_text)) > 0
      AND summary_token_count IS NOT NULL
      AND summary_route_key IS NOT NULL
      AND char_length(btrim(summary_route_key)) > 0
    )
  ),
  CONSTRAINT retention_segments_compaction_summary_ck CHECK (
    kind <> 'compaction' OR status <> 'completed' OR summary_text IS NOT NULL
  ),
  CONSTRAINT retention_segments_time_order_ck CHECK (
    updated_at >= created_at
    AND (completed_at IS NULL OR completed_at >= created_at)
  ),
  CONSTRAINT retention_segments_range_uq UNIQUE (
    conversation_id,
    epoch_floor_sequence,
    through_sequence,
    projection_version
  )
);

CREATE UNIQUE INDEX retention_segments_active_epoch_uq
  ON conversation.retention_segments (conversation_id, epoch_floor_sequence)
  WHERE kind = 'compaction'
    AND status IN ('pending', 'processing', 'retry_wait');

CREATE INDEX retention_segments_ready_idx
  ON conversation.retention_segments (next_attempt_at, segment_id)
  WHERE kind = 'compaction' AND status IN ('pending', 'retry_wait');

CREATE INDEX retention_segments_expired_lease_idx
  ON conversation.retention_segments (lease_expires_at, segment_id)
  WHERE status = 'processing';

CREATE INDEX retention_segments_projection_idx
  ON conversation.retention_segments (
    conversation_id,
    epoch_floor_sequence,
    through_sequence DESC,
    completed_at DESC
  )
  WHERE kind = 'compaction' AND status = 'completed';

CREATE INDEX retention_segments_owner_completed_idx
  ON conversation.retention_segments (
    owner_user_id,
    completed_at DESC,
    segment_id DESC
  )
  WHERE status = 'completed';

-- Python's retention_source_digest sorts every object key recursively.  jsonb's
-- native text form uses its own storage order, so hashing source_snapshot::text
-- would produce a digest that the domain model rejects after rehydration.  Keep
-- this migration-local SQL function long enough to produce the same canonical
-- representation for legacy rows.  A quoted SQL body is intentional: the
-- migration statement splitter does not need special dollar-quote handling.
CREATE FUNCTION conversation.retention_canonical_json_v1(value JSONB)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
STRICT
PARALLEL SAFE
AS '
  SELECT CASE jsonb_typeof($1)
    WHEN ''object'' THEN COALESCE(
      (
        SELECT
          ''{'' || string_agg(
            to_json(object_item.key)::TEXT || '': '' ||
            conversation.retention_canonical_json_v1(object_item.value),
            '', '' ORDER BY object_item.key COLLATE "C"
          ) || ''}''
        FROM jsonb_each($1) AS object_item
      ),
      ''{}''
    )
    WHEN ''array'' THEN COALESCE(
      (
        SELECT
          ''['' || string_agg(
            conversation.retention_canonical_json_v1(array_item.value),
            '', '' ORDER BY array_item.ordinality
          ) || '']''
        FROM jsonb_array_elements($1)
          WITH ORDINALITY AS array_item(value, ordinality)
      ),
      ''[]''
    )
    WHEN ''number'' THEN CASE
      WHEN position(''.'' IN $1::TEXT) = 0 THEN $1::TEXT
      WHEN abs(($1 #>> ''{}'')::NUMERIC) >
        1.7976931348623157e308::NUMERIC
        THEN CASE
          WHEN ($1 #>> ''{}'')::NUMERIC < 0 THEN ''-Infinity''
          ELSE ''Infinity''
        END
      WHEN to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT ~ ''[.eE]''
        THEN to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT
      ELSE to_json(($1 #>> ''{}'')::DOUBLE PRECISION)::TEXT || ''.0''
    END
    ELSE $1::TEXT
  END
';

-- Permanent snapshots were already archived facts.  Preserve their original IDs,
-- JSON arrays, summaries, and timestamps as completed legacy artifacts.  The
-- md5-derived UUID is deliberately identical to RetentionSegmentId.for_legacy_record.
INSERT INTO conversation.retention_segments (
  segment_id,
  kind,
  conversation_id,
  owner_user_id,
  epoch_floor_sequence,
  from_sequence,
  through_sequence,
  anchor_turn_id,
  predecessor_segment_id,
  projection_version,
  source_digest,
  source_snapshot,
  source_row_count,
  source_token_count,
  legacy_record_id,
  legacy_summary_raw,
  status,
  version,
  attempt_count,
  next_attempt_at,
  claim_token,
  lease_expires_at,
  completion_token,
  summary_text,
  summary_token_count,
  summary_route_key,
  last_error,
  created_at,
  updated_at,
  completed_at
)
SELECT
  md5('legacy:' || legacy.id::TEXT)::UUID,
  'legacy_archive',
  'assistant-user:' || legacy.user_id::TEXT,
  legacy.user_id,
  NULL,
  NULL,
  NULL,
  NULL,
  NULL,
  0,
  encode(
    sha256(convert_to(
      conversation.retention_canonical_json_v1(legacy.conversation_snapshot),
      'UTF8'
    )),
    'hex'
  ),
  legacy.conversation_snapshot::JSON,
  jsonb_array_length(legacy.conversation_snapshot),
  GREATEST(
    0,
    CEIL(char_length(legacy.conversation_snapshot::TEXT) / 3.0)::INTEGER
  ),
  legacy.id,
  legacy.summary,
  'completed',
  0,
  0,
  NULL,
  NULL,
  NULL,
  md5('legacy:' || legacy.id::TEXT)::UUID,
  NULLIF(btrim(legacy.summary), ''),
  CASE
    WHEN NULLIF(btrim(legacy.summary), '') IS NULL THEN NULL
    ELSE GREATEST(1, LEAST(2500, CEIL(char_length(legacy.summary) / 3.0)::INTEGER))
  END,
  CASE
    WHEN NULLIF(btrim(legacy.summary), '') IS NULL THEN NULL
    ELSE 'legacy.import:v1'
  END,
  NULL,
  legacy.created_at AT TIME ZONE 'UTC',
  legacy.created_at AT TIME ZONE 'UTC',
  legacy.created_at AT TIME ZONE 'UTC'
FROM conversation.permanent_chat_records AS legacy;

DROP FUNCTION conversation.retention_canonical_json_v1(JSONB);

-- Freeze every legacy working-history element before sequence shifting.  Invalid
-- non-array rows fail at jsonb_array_elements instead of being silently discarded.
CREATE TEMP TABLE retention_legacy_chat_import ON COMMIT DROP AS
SELECT
  chat.id AS legacy_chat_record_id,
  chat.conversation_id AS owner_user_id,
  chat.timestamp AS legacy_timestamp,
  chat.last_rotated_at AS legacy_last_rotated_at,
  element.ordinality::BIGINT AS legacy_sequence,
  element.is_empty_record,
  CASE
    WHEN element.is_empty_record THEN NULL
    WHEN jsonb_typeof(element.value) = 'object' THEN element.value
    ELSE jsonb_build_object('role', 'user', 'content', element.value)
  END AS model_message
FROM conversation.chat_records AS chat
CROSS JOIN LATERAL (
  SELECT
    expanded.value,
    expanded.ordinality,
    FALSE AS is_empty_record
  FROM jsonb_array_elements(chat.messages)
    WITH ORDINALITY AS expanded(value, ordinality)
  UNION ALL
  SELECT
    NULL::JSONB,
    1::BIGINT,
    TRUE
  WHERE jsonb_array_length(chat.messages) = 0
) AS element;

CREATE TEMP TABLE retention_legacy_chat_counts ON COMMIT DROP AS
SELECT
  'assistant-user:' || owner_user_id::TEXT AS conversation_id,
  COUNT(*)::BIGINT AS message_count
FROM retention_legacy_chat_import
GROUP BY owner_user_id;

-- Existing durable messages may already have been accepted after migration 0016.
-- Make space at the beginning so old history remains chronologically older.  Reset
-- boundaries move by the same amount, preserving their exact visibility semantics.
ALTER TABLE conversation.conversation_messages
  DROP CONSTRAINT conversation_messages_sequence_uq;

UPDATE conversation.conversation_messages AS message
SET sequence = message.sequence + legacy.message_count
FROM retention_legacy_chat_counts AS legacy
WHERE message.conversation_id = legacy.conversation_id;

UPDATE conversation.conversation_history_resets AS history_reset
SET through_sequence = history_reset.through_sequence + legacy.message_count
FROM retention_legacy_chat_counts AS legacy
WHERE history_reset.conversation_id = legacy.conversation_id;

INSERT INTO conversation.conversation_messages (
  message_id,
  conversation_id,
  sequence,
  turn_id,
  source_update_id,
  role,
  content,
  idempotency_key,
  created_at
)
SELECT
  md5(
    'legacy-chat:' || legacy_chat_record_id::TEXT || ':' || legacy_sequence::TEXT
  )::UUID,
  'assistant-user:' || owner_user_id::TEXT,
  legacy_sequence,
  NULL,
  NULL,
  CASE
    WHEN is_empty_record THEN 'system'
    WHEN model_message ->> 'role' IN ('system', 'user', 'assistant', 'tool')
      THEN model_message ->> 'role'
    ELSE 'user'
  END,
  jsonb_build_object(
    'schema_version', 1,
    'model_message', model_message,
    'imported_from', 'legacy.chat_records',
    'legacy_chat_record_id', legacy_chat_record_id,
    'legacy_empty_record', is_empty_record,
    'legacy_chat_timestamp', to_jsonb(legacy_timestamp),
    'legacy_chat_last_rotated_at', to_jsonb(legacy_last_rotated_at)
  ),
  'legacy.chat-record:' || legacy_chat_record_id::TEXT || ':' || legacy_sequence::TEXT,
  (
    COALESCE(legacy_timestamp, CURRENT_TIMESTAMP::TIMESTAMP)
    + legacy_sequence * INTERVAL '1 microsecond'
  ) AT TIME ZONE 'UTC'
FROM retention_legacy_chat_import;

ALTER TABLE conversation.conversation_messages
  ADD CONSTRAINT conversation_messages_sequence_uq
  UNIQUE (conversation_id, sequence);

-- No application dual-write is retained.  Downgrade can reconstruct the imported
-- legacy rows from their explicit provenance markers.
DROP TABLE conversation.permanent_chat_records;
DROP TABLE conversation.chat_records;

-- migrate:down

CREATE TABLE conversation.chat_records (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  conversation_id BIGINT NOT NULL UNIQUE,
  messages JSONB NOT NULL,
  timestamp TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  last_rotated_at TIMESTAMP NULL DEFAULT NULL
);

CREATE TABLE conversation.permanent_chat_records (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id BIGINT NOT NULL,
  conversation_snapshot JSONB NOT NULL,
  summary TEXT NULL DEFAULT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_permanent_user_created
  ON conversation.permanent_chat_records (user_id, created_at);

-- Restore original archive IDs and byte-equivalent JSONB values first.
INSERT INTO conversation.permanent_chat_records (
  id,
  user_id,
  conversation_snapshot,
  summary,
  created_at
)
OVERRIDING SYSTEM VALUE
SELECT
  legacy_record_id,
  owner_user_id,
  source_snapshot::JSONB,
  legacy_summary_raw,
  created_at AT TIME ZONE 'UTC'
FROM conversation.retention_segments
WHERE kind = 'legacy_archive'
ORDER BY legacy_record_id;

SELECT setval(
  pg_get_serial_sequence('conversation.permanent_chat_records', 'id'),
  GREATEST(
    COALESCE((SELECT MAX(id) FROM conversation.permanent_chat_records), 0),
    1
  ),
  TRUE
);

-- 0031 cannot represent new cumulative checkpoints.  Preserve their frozen raw
-- deltas and summaries as additional permanent archives instead of deleting data.
INSERT INTO conversation.permanent_chat_records (
  user_id,
  conversation_snapshot,
  summary,
  created_at
)
SELECT
  owner_user_id,
  source_snapshot,
  summary_text,
  created_at AT TIME ZONE 'UTC'
FROM conversation.retention_segments
WHERE kind = 'compaction' AND status = 'completed'
ORDER BY completed_at, segment_id;

-- Reconstruct only the original JSON working-history rows.  New append-only
-- messages remain in their pre-0032 table and are never duplicated on re-upgrade.
INSERT INTO conversation.chat_records (
  id,
  conversation_id,
  messages,
  timestamp,
  last_rotated_at
)
OVERRIDING SYSTEM VALUE
SELECT
  (message.content ->> 'legacy_chat_record_id')::BIGINT,
  substring(message.conversation_id FROM '^assistant-user:([0-9]+)$')::BIGINT,
  COALESCE(
    jsonb_agg(message.content -> 'model_message' ORDER BY message.sequence)
      FILTER (
        WHERE message.content ->> 'legacy_empty_record' IS DISTINCT FROM 'true'
      ),
    '[]'::JSONB
  ),
  MIN(message.content ->> 'legacy_chat_timestamp')::TIMESTAMP,
  MIN(message.content ->> 'legacy_chat_last_rotated_at')::TIMESTAMP
FROM conversation.conversation_messages AS message
WHERE message.content ->> 'imported_from' = 'legacy.chat_records'
  AND message.conversation_id ~ '^assistant-user:[0-9]+$'
GROUP BY
  message.conversation_id,
  (message.content ->> 'legacy_chat_record_id')::BIGINT;

SELECT setval(
  pg_get_serial_sequence('conversation.chat_records', 'id'),
  GREATEST(
    COALESCE((SELECT MAX(id) FROM conversation.chat_records), 0),
    1
  ),
  TRUE
);

CREATE TEMP TABLE retention_legacy_chat_counts_down ON COMMIT DROP AS
SELECT
  conversation_id,
  COUNT(*)::BIGINT AS message_count
FROM conversation.conversation_messages
WHERE content ->> 'imported_from' = 'legacy.chat_records'
GROUP BY conversation_id;

DELETE FROM conversation.conversation_messages
WHERE content ->> 'imported_from' = 'legacy.chat_records';

UPDATE conversation.conversation_messages AS message
SET sequence = message.sequence - legacy.message_count
FROM retention_legacy_chat_counts_down AS legacy
WHERE message.conversation_id = legacy.conversation_id;

UPDATE conversation.conversation_history_resets AS history_reset
SET through_sequence = history_reset.through_sequence - legacy.message_count
FROM retention_legacy_chat_counts_down AS legacy
WHERE history_reset.conversation_id = legacy.conversation_id;

DROP TABLE conversation.retention_segments;
