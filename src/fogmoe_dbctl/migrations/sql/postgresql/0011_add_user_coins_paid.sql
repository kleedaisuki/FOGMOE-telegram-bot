-- migrate:up

ALTER TABLE identity.users
  ADD COLUMN coins_paid INT NOT NULL DEFAULT 0;

-- migrate:down

ALTER TABLE identity.users
  DROP COLUMN coins_paid;
