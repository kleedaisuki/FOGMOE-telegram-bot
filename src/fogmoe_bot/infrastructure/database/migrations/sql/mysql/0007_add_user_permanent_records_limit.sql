-- migrate:up



ALTER TABLE `user` ADD COLUMN `permanent_records_limit` INT NOT NULL DEFAULT 100;



-- migrate:down



ALTER TABLE `user` DROP COLUMN `permanent_records_limit`;

