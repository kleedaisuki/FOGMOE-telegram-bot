-- migrate:up

CREATE SCHEMA IF NOT EXISTS media;

-- The preview charge and this record are committed in one transaction.  The
-- state machine makes a kill between charge, Telegram delivery, and callback
-- completion recoverable without charging twice.
CREATE TABLE media.picture_offers (
  offer_id UUID PRIMARY KEY,
  source_id TEXT NOT NULL CHECK (char_length(btrim(source_id)) BETWEEN 1 AND 255),
  sample_url TEXT,
  file_url TEXT,
  tags TEXT NOT NULL DEFAULT '',
  width INTEGER CHECK (width IS NULL OR width > 0),
  height INTEGER CHECK (height IS NULL OR height > 0),
  file_size BIGINT CHECK (file_size IS NULL OR file_size > 0),
  score INTEGER,
  rating TEXT NOT NULL CHECK (rating IN ('safe', 'nsfw')),
  requester_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  expires_at TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('preview_pending', 'available', 'charged', 'delivered', 'refunded')
  ),
  charged_user_id BIGINT
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  claim_expires_at TIMESTAMPTZ,
  preview_cost SMALLINT NOT NULL CHECK (preview_cost > 0),
  hd_cost SMALLINT CHECK (hd_cost IS NULL OR hd_cost > 0),
  preview_confirm_by TIMESTAMPTZ NOT NULL,
  preview_refunded BOOLEAN NOT NULL DEFAULT FALSE,
  hd_refunded BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (sample_url IS NOT NULL OR file_url IS NOT NULL),
  CHECK (state <> 'charged' OR charged_user_id IS NOT NULL),
  CHECK (state NOT IN ('charged', 'delivered') OR hd_cost IS NOT NULL),
  CHECK (claim_expires_at IS NULL OR state = 'charged')
);

CREATE INDEX picture_offers_recovery_idx
  ON media.picture_offers (state, preview_confirm_by, claim_expires_at, expires_at);
CREATE INDEX picture_offers_requester_created_idx
  ON media.picture_offers (requester_id, created_at DESC);

-- Callback data contains only search_id, platform, and page.  The potentially
-- long query and the exact result snapshot stay durable and below Telegram's
-- 64-byte callback_data limit.
CREATE TABLE media.music_sessions (
  search_id UUID PRIMARY KEY,
  requester_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  query TEXT NOT NULL CHECK (char_length(query) BETWEEN 1 AND 200),
  platform TEXT NOT NULL CHECK (platform IN ('wy', 'qq', 'kw', 'mg', 'qi')),
  tracks JSONB NOT NULL CHECK (jsonb_typeof(tracks) = 'array'),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX music_sessions_expiry_idx
  ON media.music_sessions (expires_at);
CREATE INDEX music_sessions_requester_created_idx
  ON media.music_sessions (requester_id, created_at DESC);

-- migrate:down

DROP TABLE IF EXISTS media.music_sessions;
DROP TABLE IF EXISTS media.picture_offers;
DROP SCHEMA IF EXISTS media;
