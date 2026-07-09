-- migrate:up



ALTER TABLE `user` ADD COLUMN `recharge_blocked_until` DATETIME NULL;



-- migrate:down



ALTER TABLE `user` DROP COLUMN `recharge_blocked_until`;

