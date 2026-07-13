-- migrate:up

-- Legacy rows cannot prove whether their owner identifier came from a private chat
-- or from one particular group. Re-projecting from the canonical Conversation log is
-- safer than guessing and accidentally crossing a privacy boundary.
TRUNCATE TABLE retrieval.source_projections, retrieval.passages CASCADE;

DROP INDEX retrieval.retrieval_source_projections_owner_idx;
DROP INDEX retrieval.retrieval_passages_owner_corpus_time_idx;

ALTER TABLE retrieval.source_projections
  DROP COLUMN owner_user_id,
  ADD COLUMN scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal','group')),
  ADD COLUMN scope_id BIGINT NOT NULL,
  ADD COLUMN personal_user_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  ADD CONSTRAINT retrieval_source_projections_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0 AND personal_user_id = scope_id)
    OR (scope_kind = 'group' AND scope_id <> 0 AND personal_user_id IS NULL)
  );

ALTER TABLE retrieval.passages
  DROP COLUMN owner_user_id,
  ADD COLUMN scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal','group')),
  ADD COLUMN scope_id BIGINT NOT NULL,
  ADD COLUMN personal_user_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  ADD CONSTRAINT retrieval_passages_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0 AND personal_user_id = scope_id)
    OR (scope_kind = 'group' AND scope_id <> 0 AND personal_user_id IS NULL)
  );

CREATE INDEX retrieval_source_projections_scope_idx
  ON retrieval.source_projections (
    scope_kind,
    scope_id,
    projected_at DESC
  );

CREATE INDEX retrieval_passages_scope_corpus_time_idx
  ON retrieval.passages (
    scope_kind,
    scope_id,
    corpus_id,
    format_version,
    occurred_at DESC,
    passage_id
  );

-- migrate:down

TRUNCATE TABLE retrieval.source_projections, retrieval.passages CASCADE;

DROP INDEX retrieval.retrieval_source_projections_scope_idx;
DROP INDEX retrieval.retrieval_passages_scope_corpus_time_idx;

ALTER TABLE retrieval.source_projections
  DROP COLUMN scope_kind,
  DROP COLUMN scope_id,
  DROP COLUMN personal_user_id,
  ADD COLUMN owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE;

ALTER TABLE retrieval.passages
  DROP COLUMN scope_kind,
  DROP COLUMN scope_id,
  DROP COLUMN personal_user_id,
  ADD COLUMN owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE;

CREATE INDEX retrieval_source_projections_owner_idx
  ON retrieval.source_projections (owner_user_id, projected_at DESC);

CREATE INDEX retrieval_passages_owner_corpus_time_idx
  ON retrieval.passages (
    owner_user_id,
    corpus_id,
    format_version,
    occurred_at DESC,
    passage_id
  );
