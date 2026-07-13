-- migrate:up

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    RAISE EXCEPTION USING
      MESSAGE = 'pgvector extension is required before migration 0041',
      HINT = 'Run CREATE EXTENSION vector as a PostgreSQL superuser, then retry.';
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS retrieval;

CREATE TABLE retrieval.embedding_spaces (
  space_id VARCHAR(100) PRIMARY KEY CHECK (
    space_id ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  model VARCHAR(255) NOT NULL CHECK (char_length(btrim(model)) > 0),
  dimensions INTEGER NOT NULL CHECK (dimensions = 1024),
  distance_metric TEXT NOT NULL CHECK (distance_metric = 'cosine'),
  query_instruction VARCHAR(2000) NOT NULL CHECK (
    char_length(btrim(query_instruction)) > 0
  ),
  passage_format_version INTEGER NOT NULL CHECK (passage_format_version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE retrieval.source_projections (
  corpus_id VARCHAR(100) NOT NULL CHECK (
    corpus_id ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  source_kind VARCHAR(100) NOT NULL CHECK (
    source_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  source_id UUID NOT NULL,
  format_version INTEGER NOT NULL CHECK (format_version > 0),
  source_digest CHAR(64) NOT NULL CHECK (
    source_digest ~ '^[0-9a-f]{64}$'
  ),
  projected_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (corpus_id, source_kind, source_id, format_version)
);

CREATE INDEX retrieval_source_projections_owner_idx
  ON retrieval.source_projections (owner_user_id, projected_at DESC);

CREATE TABLE retrieval.passages (
  passage_id UUID PRIMARY KEY,
  corpus_id VARCHAR(100) NOT NULL CHECK (
    corpus_id ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  source_kind VARCHAR(100) NOT NULL CHECK (
    source_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  source_id UUID NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  format_version INTEGER NOT NULL CHECK (format_version > 0),
  content_text TEXT NOT NULL CHECK (
    char_length(btrim(content_text)) BETWEEN 1 AND 20000
  ),
  content_digest CHAR(64) NOT NULL CHECK (
    content_digest ~ '^[0-9a-f]{64}$'
  ),
  occurred_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (corpus_id, source_kind, source_id, format_version, ordinal)
);

CREATE INDEX retrieval_passages_owner_corpus_time_idx
  ON retrieval.passages (
    owner_user_id,
    corpus_id,
    format_version,
    occurred_at DESC,
    passage_id
  );

CREATE TABLE retrieval.passage_vectors (
  passage_id UUID NOT NULL
    REFERENCES retrieval.passages(passage_id) ON DELETE CASCADE,
  space_id VARCHAR(100) NOT NULL
    REFERENCES retrieval.embedding_spaces(space_id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending','retry_wait','processing','completed','failed_final')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  embedding vector(1024),
  last_error VARCHAR(1000),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY (passage_id, space_id),
  CONSTRAINT retrieval_passage_vectors_ready_ck CHECK (
    (status IN ('pending','retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT retrieval_passage_vectors_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT retrieval_passage_vectors_result_ck CHECK (
    (status = 'completed') = (
      embedding IS NOT NULL AND completed_at IS NOT NULL
    )
  )
);

CREATE INDEX retrieval_passage_vectors_ready_idx
  ON retrieval.passage_vectors (space_id, next_attempt_at, passage_id)
  WHERE status IN ('pending','retry_wait');

CREATE INDEX retrieval_passage_vectors_expired_lease_idx
  ON retrieval.passage_vectors (space_id, lease_expires_at, passage_id)
  WHERE status = 'processing';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM memory.records AS record
    WHERE NOT EXISTS (
      SELECT 1
      FROM conversation.conversation_messages AS message
      WHERE message.conversation_id = record.conversation_id
    )
  ) THEN
    RAISE EXCEPTION 'cannot remove memory.records: an archive has no Conversation source';
  END IF;
END
$$;

DROP TABLE memory.records;

-- migrate:down

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

DROP SCHEMA retrieval CASCADE;
