-- migrate:up

CREATE TABLE economy.stake_reward_pool (
  id SMALLINT NOT NULL PRIMARY KEY,
  balance DECIMAL(20,2) NOT NULL DEFAULT 0,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO economy.stake_reward_pool (id, balance) VALUES (1, 0);

-- migrate:down

DROP TABLE IF EXISTS economy.stake_reward_pool;
