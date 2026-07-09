-- migrate:up

ALTER TABLE identity.users
  ADD COLUMN permanent_records_limit INT NOT NULL DEFAULT 100;

-- migrate:down

ALTER TABLE identity.users
  DROP COLUMN permanent_records_limit;
