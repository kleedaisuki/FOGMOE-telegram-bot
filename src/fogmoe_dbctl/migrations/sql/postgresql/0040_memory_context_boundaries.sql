-- migrate:up

CREATE SCHEMA IF NOT EXISTS context_window;
CREATE SCHEMA IF NOT EXISTS memory;

CREATE TABLE memory.records (
  memory_id UUID PRIMARY KEY,
  owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL CHECK (
    char_length(conversation_id) BETWEEN 1 AND 512
  ),
  source_kind TEXT NOT NULL CHECK (
    source_kind IN ('compaction_checkpoint', 'legacy_archive')
  ),
  source_id UUID NOT NULL UNIQUE,
  source_digest CHAR(64) NOT NULL CHECK (
    source_digest ~ '^[0-9a-f]{64}$'
  ),
  snapshot JSON NOT NULL CHECK (json_typeof(snapshot) = 'array'),
  summary_text TEXT,
  legacy_record_id BIGINT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT memory_records_summary_ck CHECK (
    summary_text IS NULL OR char_length(btrim(summary_text)) > 0
  )
);

CREATE INDEX memory_records_owner_created_idx
  ON memory.records (owner_user_id, created_at DESC, memory_id DESC);

INSERT INTO memory.records (
  memory_id,
  owner_user_id,
  conversation_id,
  source_kind,
  source_id,
  source_digest,
  snapshot,
  summary_text,
  legacy_record_id,
  created_at
)
SELECT
  segment_id,
  owner_user_id,
  conversation_id,
  CASE kind
    WHEN 'compaction' THEN 'compaction_checkpoint'
    ELSE 'legacy_archive'
  END,
  segment_id,
  source_digest,
  source_snapshot,
  summary_text,
  legacy_record_id,
  COALESCE(completed_at, created_at)
FROM conversation.retention_segments
WHERE status = 'completed';

DELETE FROM conversation.retention_segments
WHERE kind = 'legacy_archive';

DROP INDEX conversation.retention_segments_owner_completed_idx;
DROP INDEX conversation.retention_segments_projection_idx;
DROP INDEX conversation.retention_segments_expired_lease_idx;
DROP INDEX conversation.retention_segments_ready_idx;
DROP INDEX conversation.retention_segments_active_epoch_uq;

ALTER TABLE conversation.retention_segments
  DROP CONSTRAINT retention_segments_kind_shape_ck,
  DROP CONSTRAINT retention_segments_compaction_summary_ck;

ALTER TABLE conversation.retention_segments SET SCHEMA context_window;
ALTER TABLE context_window.retention_segments RENAME TO compactions;

ALTER TABLE context_window.compactions
  RENAME COLUMN segment_id TO compaction_id;
ALTER TABLE context_window.compactions
  RENAME COLUMN predecessor_segment_id TO predecessor_compaction_id;

ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_pkey TO compactions_pkey;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_range_uq TO compactions_range_uq;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_claimable_time_ck
    TO compactions_claimable_time_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_lease_ck TO compactions_lease_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_claim_pair_ck TO compactions_claim_pair_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_terminal_time_ck
    TO compactions_terminal_time_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_completion_token_ck
    TO compactions_completion_token_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_summary_shape_ck
    TO compactions_summary_shape_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT retention_segments_time_order_ck TO compactions_time_order_ck;

ALTER TABLE context_window.compactions
  DROP COLUMN kind,
  DROP COLUMN legacy_record_id,
  DROP COLUMN legacy_summary_raw,
  ALTER COLUMN epoch_floor_sequence SET NOT NULL,
  ALTER COLUMN from_sequence SET NOT NULL,
  ALTER COLUMN through_sequence SET NOT NULL,
  ALTER COLUMN anchor_turn_id SET NOT NULL;

ALTER TABLE context_window.compactions
  ADD CONSTRAINT compactions_projection_version_ck CHECK (projection_version > 0),
  ADD CONSTRAINT compactions_source_ck CHECK (
    source_row_count > 0 AND json_array_length(source_snapshot) > 0
  ),
  ADD CONSTRAINT compactions_range_ck CHECK (
    epoch_floor_sequence >= 0
    AND from_sequence > epoch_floor_sequence
    AND through_sequence >= from_sequence
  ),
  ADD CONSTRAINT compactions_completed_summary_ck CHECK (
    status <> 'completed' OR summary_text IS NOT NULL
  );

CREATE UNIQUE INDEX compactions_active_epoch_uq
  ON context_window.compactions (conversation_id, epoch_floor_sequence)
  WHERE status IN ('pending', 'processing', 'retry_wait');

CREATE INDEX compactions_ready_idx
  ON context_window.compactions (next_attempt_at, compaction_id)
  WHERE status IN ('pending', 'retry_wait');

CREATE INDEX compactions_expired_lease_idx
  ON context_window.compactions (lease_expires_at, compaction_id)
  WHERE status = 'processing';

CREATE INDEX compactions_projection_idx
  ON context_window.compactions (
    conversation_id,
    epoch_floor_sequence,
    through_sequence DESC,
    completed_at DESC
  )
  WHERE status = 'completed';

-- migrate:down

ALTER TABLE context_window.compactions
  DROP CONSTRAINT compactions_completed_summary_ck,
  DROP CONSTRAINT compactions_range_ck,
  DROP CONSTRAINT compactions_source_ck,
  DROP CONSTRAINT compactions_projection_version_ck;

DROP INDEX context_window.compactions_projection_idx;
DROP INDEX context_window.compactions_expired_lease_idx;
DROP INDEX context_window.compactions_ready_idx;
DROP INDEX context_window.compactions_active_epoch_uq;

ALTER TABLE context_window.compactions
  ADD COLUMN kind TEXT NOT NULL DEFAULT 'compaction'
    CHECK (kind IN ('compaction', 'legacy_archive')),
  ADD COLUMN legacy_record_id BIGINT UNIQUE,
  ADD COLUMN legacy_summary_raw TEXT;

ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_pkey TO retention_segments_pkey;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_range_uq TO retention_segments_range_uq;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_claimable_time_ck
    TO retention_segments_claimable_time_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_lease_ck TO retention_segments_lease_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_claim_pair_ck TO retention_segments_claim_pair_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_terminal_time_ck
    TO retention_segments_terminal_time_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_completion_token_ck
    TO retention_segments_completion_token_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_summary_shape_ck
    TO retention_segments_summary_shape_ck;
ALTER TABLE context_window.compactions
  RENAME CONSTRAINT compactions_time_order_ck TO retention_segments_time_order_ck;

ALTER TABLE context_window.compactions
  RENAME COLUMN predecessor_compaction_id TO predecessor_segment_id;
ALTER TABLE context_window.compactions
  RENAME COLUMN compaction_id TO segment_id;
ALTER TABLE context_window.compactions RENAME TO retention_segments;
ALTER TABLE context_window.retention_segments SET SCHEMA conversation;

ALTER TABLE conversation.retention_segments
  ALTER COLUMN epoch_floor_sequence DROP NOT NULL,
  ALTER COLUMN from_sequence DROP NOT NULL,
  ALTER COLUMN through_sequence DROP NOT NULL,
  ALTER COLUMN anchor_turn_id DROP NOT NULL,
  ADD CONSTRAINT retention_segments_kind_shape_ck CHECK (
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
  ADD CONSTRAINT retention_segments_compaction_summary_ck CHECK (
    kind <> 'compaction' OR status <> 'completed' OR summary_text IS NOT NULL
  );

INSERT INTO conversation.retention_segments (
  segment_id,
  kind,
  conversation_id,
  owner_user_id,
  projection_version,
  source_digest,
  source_snapshot,
  source_row_count,
  source_token_count,
  legacy_record_id,
  status,
  completion_token,
  summary_text,
  summary_token_count,
  summary_route_key,
  created_at,
  updated_at,
  completed_at
)
SELECT
  memory_id,
  'legacy_archive',
  conversation_id,
  owner_user_id,
  0,
  source_digest,
  snapshot,
  json_array_length(snapshot),
  GREATEST(0, CEIL(char_length(snapshot::TEXT) / 3.0)::INTEGER),
  legacy_record_id,
  'completed',
  memory_id,
  summary_text,
  CASE
    WHEN summary_text IS NULL THEN NULL
    ELSE GREATEST(1, LEAST(2500, CEIL(char_length(summary_text) / 3.0)::INTEGER))
  END,
  CASE WHEN summary_text IS NULL THEN NULL ELSE 'memory.downgrade:v1' END,
  created_at,
  created_at,
  created_at
FROM memory.records
WHERE source_kind = 'legacy_archive';

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

DROP TABLE memory.records;
DROP SCHEMA memory;
DROP SCHEMA context_window;
