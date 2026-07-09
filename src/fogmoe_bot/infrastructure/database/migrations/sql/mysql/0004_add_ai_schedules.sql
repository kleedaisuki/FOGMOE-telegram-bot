-- migrate:up



CREATE TABLE IF NOT EXISTS `ai_schedules` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `run_at` DATETIME NOT NULL,
  `trigger_reason` VARCHAR(200) NOT NULL,
  `context` TEXT NULL,
  `prompt` TEXT NOT NULL,
  `status` ENUM('pending','executing','executed','cancelled','failed') NOT NULL DEFAULT 'pending',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `executed_at` TIMESTAMP NULL DEFAULT NULL,
  `error` TEXT NULL,
  PRIMARY KEY (`id`),
  INDEX `idx_ai_schedules_user_status` (`user_id`, `status`),
  INDEX `idx_ai_schedules_run` (`status`, `run_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;



-- migrate:down



DROP TABLE IF EXISTS `ai_schedules`;

