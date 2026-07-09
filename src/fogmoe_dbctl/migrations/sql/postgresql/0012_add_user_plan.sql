-- migrate:up

ALTER TABLE identity.users
  ADD COLUMN user_plan VARCHAR(10) NOT NULL DEFAULT 'free';

UPDATE identity.users SET user_plan = 'paid' WHERE coins_paid > 0;

UPDATE identity.users SET user_plan = 'admin' WHERE id = {{ admin_user_id }};

-- migrate:down

ALTER TABLE identity.users
  DROP COLUMN user_plan;
