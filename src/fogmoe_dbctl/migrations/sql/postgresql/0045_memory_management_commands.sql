-- migrate:up

-- A deletion without an ingestion boundary is temporary: an asynchronous projector can
-- recreate the same vector immediately.  Keep one monotonic boundary per privacy scope;
-- source discovery and projection both enforce it.
CREATE TABLE retrieval.scope_forgetting_boundaries (
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal','group')),
  scope_id BIGINT NOT NULL,
  personal_user_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  forgotten_through TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT retrieval_scope_forgetting_boundaries_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0 AND personal_user_id = scope_id)
    OR (scope_kind = 'group' AND scope_id <> 0 AND personal_user_id IS NULL)
  ),
  CONSTRAINT retrieval_scope_forgetting_boundaries_time_ck CHECK (
    updated_at >= created_at AND forgotten_through <= updated_at
  ),
  PRIMARY KEY (scope_kind, scope_id)
);

-- Profile clearing has a distinct boundary from RAG memory.  It fences delayed evidence
-- projection while preserving Conversation and retrieval data.
ALTER TABLE user_profile.profiles
  ADD COLUMN forgotten_through TIMESTAMPTZ;

-- Telegram membership is external and mutable.  Freeze the first observation so an
-- at-least-once inbox replay cannot change a destructive command's authorization.
CREATE TABLE conversation.command_authorization_decisions (
  source_update_id BIGINT NOT NULL
    REFERENCES conversation.inbound_updates(update_id) ON DELETE CASCADE,
  capability VARCHAR(100) NOT NULL CHECK (
    capability ~ '^[a-z][a-z0-9_.-]{0,99}$'
  ),
  resource_id BIGINT NOT NULL CHECK (resource_id <> 0),
  actor_user_id BIGINT NOT NULL CHECK (actor_user_id > 0),
  allowed BOOLEAN NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source_update_id, capability)
);

-- migrate:down

DROP TABLE conversation.command_authorization_decisions;

ALTER TABLE user_profile.profiles
  DROP COLUMN forgotten_through;

DROP TABLE retrieval.scope_forgetting_boundaries;
