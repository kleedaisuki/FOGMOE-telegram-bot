-- migrate:up



CREATE TABLE `stake_reward_pool` (
  `id` TINYINT NOT NULL,
  `balance` DECIMAL(20,2) NOT NULL DEFAULT 0,
  `updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `stake_reward_pool` (`id`, `balance`) VALUES (1, 0);



-- migrate:down



DROP TABLE IF EXISTS `stake_reward_pool`;

