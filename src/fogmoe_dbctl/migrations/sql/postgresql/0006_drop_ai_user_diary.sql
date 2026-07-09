-- migrate:up

DROP TABLE IF EXISTS conversation.ai_user_diary;

-- migrate:down

CREATE TABLE IF NOT EXISTS conversation.ai_user_diary (
  user_id BIGINT NOT NULL PRIMARY KEY,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
);
