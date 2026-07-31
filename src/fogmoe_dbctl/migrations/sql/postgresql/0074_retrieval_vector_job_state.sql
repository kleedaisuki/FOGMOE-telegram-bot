-- migrate:up

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '5min';

LOCK TABLE retrieval.passage_vectors IN SHARE ROW EXCLUSIVE MODE;

DO $vector_job_preflight$
DECLARE
  incompatible_count BIGINT;
BEGIN
  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'processing'
    AND (claim_token IS NULL OR lease_expires_at IS NULL);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s processing row(s) lack complete lease ownership',
        incompatible_count
      ),
      HINT = 'Resolve the ambiguous claim from authoritative worker evidence before retrying migration 0074; migration does not invent a token or deadline.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'completed'
    AND (embedding IS NULL OR completed_at IS NULL);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s completed row(s) lack complete result fields',
        incompatible_count
      ),
      HINT = 'Rebuild or reconcile the result from its authoritative passage before retrying migration 0074; migration does not fabricate an embedding or completion time.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status <> 'completed'
    AND (embedding IS NOT NULL OR completed_at IS NOT NULL);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s non-completed row(s) retain result fields',
        incompatible_count
      ),
      HINT = 'Inspect the authoritative embedding outcome and repair its status/result shape before retrying migration 0074; no vector is cleared automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'completed' AND vector_norm(embedding) = 0;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s completed row(s) contain a zero vector',
        incompatible_count
      ),
      HINT = 'Rebuild the embedding from its authoritative passage before retrying migration 0074; completed vectors are never cleared or replaced automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors AS vector
  JOIN retrieval.passages AS passage
    ON passage.passage_id = vector.passage_id
  JOIN retrieval.embedding_spaces AS space
    ON space.space_id = vector.space_id
  WHERE passage.format_version <> space.passage_format_version;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s row(s) bind incompatible passage and space formats',
        incompatible_count
      ),
      HINT = 'Reconcile the passage projection and embedding-space identity before retrying migration 0074; vector-job keys are not rewritten automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status IN ('retry_wait', 'failed_final')
    AND (last_error IS NULL OR char_length(btrim(last_error)) = 0);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s failure-state row(s) lack an error summary',
        incompatible_count
      ),
      HINT = 'Recover the original failure reason from operational evidence before retrying migration 0074; no reason is fabricated automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status IN ('pending', 'processing', 'completed')
    AND last_error IS NOT NULL;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s non-failure row(s) retain an error summary',
        incompatible_count
      ),
      HINT = 'Archive or explicitly classify the retained diagnostic before retrying migration 0074; no error text is discarded automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE (status = 'pending' AND attempt_count <> 0)
    OR (status <> 'pending' AND attempt_count = 0);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s row(s) violate state-specific attempt counts',
        incompatible_count
      ),
      HINT = 'Reconcile attempt_count with audited claim history before retrying migration 0074; counters are not rewritten automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE version < attempt_count;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s row(s) have version below attempt_count',
        incompatible_count
      ),
      HINT = 'Reconcile the optimistic version with audited transition history before retrying migration 0074; no version formula is inferred.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE created_at > updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s row(s) have updated_at before created_at',
        incompatible_count
      ),
      HINT = 'Repair timestamps from authoritative records before retrying migration 0074; timestamps are not reordered automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'pending'
    AND (
      next_attempt_at IS DISTINCT FROM created_at
      OR updated_at IS DISTINCT FROM created_at
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s pending row(s) have noncanonical initial timestamps',
        incompatible_count
      ),
      HINT = 'Verify the original enqueue instant before retrying migration 0074; pending schedules are not changed automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'processing' AND lease_expires_at <= updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s processing row(s) have a nonfuture lease',
        incompatible_count
      ),
      HINT = 'Resolve the active claim through the normal recovery workflow before retrying migration 0074; migration does not steal leases.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'retry_wait' AND next_attempt_at < updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s retry row(s) are scheduled before updated_at',
        incompatible_count
      ),
      HINT = 'Repair the retry schedule from authoritative failure timing before retrying migration 0074; schedules are not shifted automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM retrieval.passage_vectors
  WHERE status = 'completed' AND completed_at IS DISTINCT FROM updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'retrieval vector preflight: %s completed row(s) have divergent completion timestamps',
        incompatible_count
      ),
      HINT = 'Reconcile completion timestamps from authoritative records before retrying migration 0074; neither timestamp is guessed.';
  END IF;
END
$vector_job_preflight$;

UPDATE retrieval.passage_vectors
SET claim_token = NULL, lease_expires_at = NULL
WHERE status <> 'processing'
  AND (claim_token IS NOT NULL OR lease_expires_at IS NOT NULL);

ALTER TABLE retrieval.passage_vectors
  DROP CONSTRAINT retrieval_passage_vectors_lease_ck;

ALTER TABLE retrieval.passage_vectors
  ADD CONSTRAINT retrieval_passage_vectors_lease_ck CHECK (
    (
      status = 'processing'
      AND claim_token IS NOT NULL
      AND lease_expires_at IS NOT NULL
    )
    OR (
      status <> 'processing'
      AND claim_token IS NULL
      AND lease_expires_at IS NULL
    )
  ) NOT VALID;

ALTER TABLE retrieval.passage_vectors
  VALIDATE CONSTRAINT retrieval_passage_vectors_lease_ck;

ALTER TABLE retrieval.passage_vectors
  DROP CONSTRAINT retrieval_passage_vectors_result_ck;

ALTER TABLE retrieval.passage_vectors
  ADD CONSTRAINT retrieval_passage_vectors_result_ck CHECK (
    (
      status = 'completed'
      AND embedding IS NOT NULL
      AND completed_at IS NOT NULL
    )
    OR (
      status <> 'completed'
      AND embedding IS NULL
      AND completed_at IS NULL
    )
  ) NOT VALID;

ALTER TABLE retrieval.passage_vectors
  VALIDATE CONSTRAINT retrieval_passage_vectors_result_ck;

ALTER TABLE retrieval.passage_vectors
  ADD CONSTRAINT retrieval_passage_vectors_error_ck CHECK (
    (
      status IN ('retry_wait', 'failed_final')
      AND last_error IS NOT NULL
      AND char_length(btrim(last_error)) > 0
    )
    OR (
      status IN ('pending', 'processing', 'completed')
      AND last_error IS NULL
    )
  ) NOT VALID;

ALTER TABLE retrieval.passage_vectors
  VALIDATE CONSTRAINT retrieval_passage_vectors_error_ck;

-- migrate:down

ALTER TABLE retrieval.passage_vectors
  DROP CONSTRAINT retrieval_passage_vectors_error_ck;

ALTER TABLE retrieval.passage_vectors
  DROP CONSTRAINT retrieval_passage_vectors_result_ck;

ALTER TABLE retrieval.passage_vectors
  ADD CONSTRAINT retrieval_passage_vectors_result_ck CHECK (
    (status = 'completed') = (
      embedding IS NOT NULL AND completed_at IS NOT NULL
    )
  );

ALTER TABLE retrieval.passage_vectors
  DROP CONSTRAINT retrieval_passage_vectors_lease_ck;

ALTER TABLE retrieval.passage_vectors
  ADD CONSTRAINT retrieval_passage_vectors_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  );
