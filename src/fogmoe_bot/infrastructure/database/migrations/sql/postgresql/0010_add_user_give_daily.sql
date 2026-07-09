-- migrate:up

CREATE TABLE economy.user_give_daily (
  user_id BIGINT NOT NULL,
  give_date DATE NOT NULL,
  give_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, give_date)
);

-- migrate:down

DROP TABLE IF EXISTS economy.user_give_daily;
