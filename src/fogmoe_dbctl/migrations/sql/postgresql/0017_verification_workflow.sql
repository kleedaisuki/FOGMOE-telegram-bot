-- migrate:up

ALTER TABLE moderation.verification_tasks
  ALTER COLUMN message_id DROP NOT NULL;

UPDATE moderation.verification_tasks
SET token_hash = repeat('0', 64)
WHERE token_hash IS NULL OR token_hash !~ '^[0-9a-f]{64}$';

ALTER TABLE moderation.verification_tasks
  ALTER COLUMN token_hash SET NOT NULL,
  ALTER COLUMN expire_time TYPE TIMESTAMPTZ
    USING expire_time AT TIME ZONE 'UTC';

ALTER TABLE moderation.verification_tasks
  ADD COLUMN status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN member_name TEXT NOT NULL DEFAULT '用户',
  ADD COLUMN version BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN next_attempt_at TIMESTAMPTZ,
  ADD COLUMN claim_token UUID,
  ADD COLUMN lease_expires_at TIMESTAMPTZ,
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0,
  ADD COLUMN last_error TEXT,
  ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

UPDATE moderation.verification_tasks
SET next_attempt_at = expire_time;

ALTER TABLE moderation.verification_tasks
  ADD CONSTRAINT verification_tasks_status_ck CHECK (
    status IN (
      'creating', 'pending', 'passing', 'expiring', 'cancelling',
      'passed', 'expired', 'cancelled'
    )
  ),
  ADD CONSTRAINT verification_tasks_version_ck CHECK (version >= 0),
  ADD CONSTRAINT verification_tasks_attempt_count_ck CHECK (attempt_count >= 0),
  ADD CONSTRAINT verification_tasks_token_hash_ck CHECK (
    token_hash ~ '^[0-9a-f]{64}$'
  ),
  ADD CONSTRAINT verification_tasks_lease_ck CHECK (
    (claim_token IS NULL) = (lease_expires_at IS NULL)
  ),
  ADD CONSTRAINT verification_tasks_message_ck CHECK (
    message_id IS NOT NULL OR status IN ('creating', 'cancelling', 'cancelled')
  ),
  ADD CONSTRAINT verification_tasks_schedule_ck CHECK (
    (status IN ('passed', 'expired', 'cancelled')) = (next_attempt_at IS NULL)
  );

CREATE INDEX verification_tasks_ready_idx
  ON moderation.verification_tasks (next_attempt_at, group_id, user_id)
  WHERE status IN ('creating', 'pending', 'passing', 'expiring', 'cancelling')
    AND claim_token IS NULL;

CREATE INDEX verification_tasks_expired_lease_idx
  ON moderation.verification_tasks (lease_expires_at, group_id, user_id)
  WHERE claim_token IS NOT NULL;

-- migrate:down

DROP INDEX IF EXISTS moderation.verification_tasks_expired_lease_idx;
DROP INDEX IF EXISTS moderation.verification_tasks_ready_idx;

ALTER TABLE moderation.verification_tasks
  DROP CONSTRAINT IF EXISTS verification_tasks_schedule_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_message_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_lease_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_token_hash_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_attempt_count_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_version_ck,
  DROP CONSTRAINT IF EXISTS verification_tasks_status_ck;

DELETE FROM moderation.verification_tasks WHERE message_id IS NULL;

ALTER TABLE moderation.verification_tasks
  DROP COLUMN IF EXISTS updated_at,
  DROP COLUMN IF EXISTS created_at,
  DROP COLUMN IF EXISTS last_error,
  DROP COLUMN IF EXISTS attempt_count,
  DROP COLUMN IF EXISTS lease_expires_at,
  DROP COLUMN IF EXISTS claim_token,
  DROP COLUMN IF EXISTS next_attempt_at,
  DROP COLUMN IF EXISTS version,
  DROP COLUMN IF EXISTS member_name,
  DROP COLUMN IF EXISTS status,
  ALTER COLUMN expire_time TYPE TIMESTAMP
    USING expire_time AT TIME ZONE 'UTC',
  ALTER COLUMN message_id SET NOT NULL;
