-- migrate:up



ALTER TABLE `user` ADD COLUMN `user_plan` VARCHAR(10) NOT NULL DEFAULT 'free';

UPDATE user SET user_plan = 'paid' WHERE coins_paid > 0;

UPDATE user SET user_plan = 'admin' WHERE id = {{ admin_user_id }};



-- migrate:down



ALTER TABLE `user` DROP COLUMN `user_plan`;

