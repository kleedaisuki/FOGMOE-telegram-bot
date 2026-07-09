-- migrate:up

CREATE TABLE IF NOT EXISTS conversation.ai_user_diary_pages (
  user_id BIGINT NOT NULL,
  page_no INT NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, page_no)
);

INSERT INTO conversation.ai_user_diary_pages (user_id, page_no, content, created_at, updated_at)
SELECT user_id, 1, content, created_at, updated_at
FROM conversation.ai_user_diary
WHERE content IS NOT NULL AND content != ''
ON CONFLICT (user_id, page_no) DO UPDATE SET
  content = EXCLUDED.content,
  updated_at = EXCLUDED.updated_at;

-- migrate:down

DROP TABLE IF EXISTS conversation.ai_user_diary_pages;
