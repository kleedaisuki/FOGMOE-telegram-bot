-- migrate:up

CREATE SCHEMA IF NOT EXISTS admin;

-- The source Update is the intent's stable idempotency identity.  Audience
-- membership is materialized in the same transaction so restarts and replicas
-- cannot disagree about whom this particular announcement targets.
CREATE TABLE admin.announcements (
  announcement_id UUID PRIMARY KEY,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 255),
  requested_by BIGINT NOT NULL CHECK (requested_by > 0),
  source_update_id BIGINT NOT NULL UNIQUE
    REFERENCES conversation.inbound_updates(update_id) ON DELETE RESTRICT,
  body TEXT NOT NULL
    CHECK (char_length(btrim(body)) BETWEEN 1 AND 3500),
  recipient_count INTEGER NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
  state TEXT NOT NULL DEFAULT 'expanding'
    CHECK (state IN ('expanding', 'delivering', 'completed')),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  CONSTRAINT admin_announcements_time_order_ck CHECK (updated_at >= created_at),
  CONSTRAINT admin_announcements_completed_shape_ck CHECK (
    (state = 'completed') = (completed_at IS NOT NULL)
  )
);

CREATE INDEX admin_announcements_state_created_idx
  ON admin.announcements (state, created_at, announcement_id);

-- Each row is both a durable audience snapshot entry and a recoverable
-- expansion receipt.  The special completion row is blocked until every
-- audience outbox has reached a terminal delivery state, preserving the old
-- administrator completion report without coupling Update handling to fanout.
CREATE TABLE admin.announcement_recipients (
  announcement_id UUID NOT NULL
    REFERENCES admin.announcements(announcement_id) ON DELETE CASCADE,
  recipient_kind TEXT NOT NULL
    CHECK (recipient_kind IN ('user', 'group', 'completion')),
  chat_id BIGINT NOT NULL CHECK (chat_id <> 0),
  message_thread_id BIGINT CHECK (
    message_thread_id IS NULL OR message_thread_id > 0
  ),
  reply_to_message_id BIGINT CHECK (
    reply_to_message_id IS NULL OR reply_to_message_id > 0
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'blocked',
      'pending',
      'processing',
      'retry_wait',
      'expanded',
      'failed_final'
    )
  ),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  outbound_message_id UUID UNIQUE
    REFERENCES conversation.outbound_messages(message_id) ON DELETE RESTRICT,
  last_error VARCHAR(100),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  expanded_at TIMESTAMPTZ,
  terminal_at TIMESTAMPTZ,
  PRIMARY KEY (announcement_id, recipient_kind, chat_id),
  CONSTRAINT admin_announcement_recipients_address_ck CHECK (
    (recipient_kind = 'completion') = (reply_to_message_id IS NOT NULL)
  ),
  CONSTRAINT admin_announcement_recipients_blocked_ck CHECK (
    status <> 'blocked' OR recipient_kind = 'completion'
  ),
  CONSTRAINT admin_announcement_recipients_claimable_ck CHECK (
    (status IN ('pending', 'retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT admin_announcement_recipients_lease_ck CHECK (
    (status = 'processing') =
    (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CONSTRAINT admin_announcement_recipients_expanded_ck CHECK (
    (status = 'expanded') =
    (outbound_message_id IS NOT NULL AND expanded_at IS NOT NULL)
  ),
  CONSTRAINT admin_announcement_recipients_failed_ck CHECK (
    (status = 'failed_final') = (terminal_at IS NOT NULL)
  ),
  CONSTRAINT admin_announcement_recipients_time_order_ck CHECK (
    updated_at >= created_at
  )
);

CREATE INDEX admin_announcement_recipients_ready_idx
  ON admin.announcement_recipients
    (next_attempt_at, announcement_id, recipient_kind, chat_id)
  WHERE status IN ('pending', 'retry_wait');

CREATE INDEX admin_announcement_recipients_expired_idx
  ON admin.announcement_recipients
    (lease_expires_at, announcement_id, recipient_kind, chat_id)
  WHERE status = 'processing';

-- migrate:down

DROP TABLE admin.announcement_recipients;
DROP TABLE admin.announcements;
DROP SCHEMA admin;
