-- migrate:up

ALTER TABLE assistant.ai_schedules
  ADD COLUMN IF NOT EXISTS claim_token UUID,
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP;

UPDATE assistant.ai_schedules
SET lease_expires_at = CURRENT_TIMESTAMP
WHERE status = 'executing' AND lease_expires_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_ai_schedules_lease
  ON assistant.ai_schedules (status, lease_expires_at);

-- migrate:down

DROP INDEX IF EXISTS assistant.idx_ai_schedules_lease;

ALTER TABLE assistant.ai_schedules
  DROP COLUMN IF EXISTS lease_expires_at,
  DROP COLUMN IF EXISTS claim_token;
