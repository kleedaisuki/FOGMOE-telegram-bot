-- migrate:up



CREATE TABLE `user_give_daily` (
  `user_id` BIGINT NOT NULL,
  `give_date` DATE NOT NULL,
  `give_count` INT NOT NULL DEFAULT 0,
  PRIMARY KEY (`user_id`, `give_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;



-- migrate:down



DROP TABLE IF EXISTS `user_give_daily`;

