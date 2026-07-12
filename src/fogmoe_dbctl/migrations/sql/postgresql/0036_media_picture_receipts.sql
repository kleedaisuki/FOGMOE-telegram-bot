-- migrate:up

-- The source receipt, preview charge, offer, and Telegram photo outbox row are
-- committed together.  The JSON snapshot deliberately survives bounded offer
-- cleanup so a late replay still returns the first canonical result.
CREATE TABLE media.picture_request_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  requester_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  rating TEXT NOT NULL CHECK (rating IN ('safe', 'nsfw')),
  request_fingerprint CHAR(64) NOT NULL CHECK (
    request_fingerprint ~ '^[0-9a-f]{64}$'
  ),
  offer_id UUID NOT NULL UNIQUE,
  outbound_message_id UUID NOT NULL UNIQUE
    REFERENCES conversation.outbound_messages(message_id) ON DELETE RESTRICT,
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX picture_request_receipts_requester_created_idx
  ON media.picture_request_receipts
    (requester_id, created_at DESC, idempotency_key);

-- migrate:down

DROP TABLE media.picture_request_receipts;
