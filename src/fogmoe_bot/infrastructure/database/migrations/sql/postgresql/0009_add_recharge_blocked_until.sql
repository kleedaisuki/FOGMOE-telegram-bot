-- migrate:up

ALTER TABLE identity.users
  ADD COLUMN recharge_blocked_until TIMESTAMP NULL;

-- migrate:down

ALTER TABLE identity.users
  DROP COLUMN recharge_blocked_until;
