-- migrate:up

ALTER TABLE identity.users
  DROP COLUMN permanent_records_limit;

DROP SCHEMA memory;

-- migrate:down

CREATE SCHEMA memory;

ALTER TABLE identity.users
  ADD COLUMN permanent_records_limit INT NOT NULL DEFAULT 100;
