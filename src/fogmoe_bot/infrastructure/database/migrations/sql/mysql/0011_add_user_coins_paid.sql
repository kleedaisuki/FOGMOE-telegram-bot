-- migrate:up



ALTER TABLE `user` ADD COLUMN `coins_paid` INT NOT NULL DEFAULT 0;



-- migrate:down



ALTER TABLE `user` DROP COLUMN `coins_paid`;

