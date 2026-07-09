-- migrate:up

ALTER TABLE conversation.chat_records
  ADD COLUMN last_rotated_at TIMESTAMP NULL DEFAULT NULL;

-- migrate:down

ALTER TABLE conversation.chat_records
  DROP COLUMN last_rotated_at;
