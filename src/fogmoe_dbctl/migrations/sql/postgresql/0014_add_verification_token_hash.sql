-- migrate:up

ALTER TABLE moderation.verification_tasks
  ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64);

-- migrate:down

ALTER TABLE moderation.verification_tasks
  DROP COLUMN IF EXISTS token_hash;
