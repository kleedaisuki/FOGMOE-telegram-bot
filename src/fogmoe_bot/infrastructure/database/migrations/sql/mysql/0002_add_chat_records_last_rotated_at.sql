-- migrate:up



ALTER TABLE `chat_records` ADD COLUMN `last_rotated_at` TIMESTAMP NULL DEFAULT NULL;



-- migrate:down



ALTER TABLE `chat_records` DROP COLUMN `last_rotated_at`;

