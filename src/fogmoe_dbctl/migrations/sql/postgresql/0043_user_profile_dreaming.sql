-- migrate:up

CREATE SCHEMA user_profile;

CREATE TABLE user_profile.evidence_events (
  event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_turn_id UUID NOT NULL UNIQUE
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE CASCADE,
  owner_user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  user_text TEXT NOT NULL CHECK (
    char_length(btrim(user_text)) BETWEEN 1 AND 100000
  ),
  assistant_text TEXT NOT NULL CHECK (
    char_length(btrim(assistant_text)) BETWEEN 1 AND 100000
  ),
  occurred_at TIMESTAMPTZ NOT NULL,
  metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
  source_digest CHAR(64) NOT NULL CHECK (
    source_digest ~ '^[0-9a-f]{64}$'
  ),
  projected_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX user_profile_evidence_owner_event_idx
  ON user_profile.evidence_events (owner_user_id, event_id);

CREATE TABLE user_profile.profiles (
  user_id BIGINT PRIMARY KEY
    REFERENCES identity.users(id) ON DELETE CASCADE,
  current_revision BIGINT,
  observed_through_event_id BIGINT NOT NULL DEFAULT 0 CHECK (
    observed_through_event_id >= 0
  ),
  next_eligible_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT user_profiles_revision_ck CHECK (
    current_revision IS NULL OR current_revision > 0
  ),
  CONSTRAINT user_profiles_time_ck CHECK (updated_at >= created_at)
);

CREATE INDEX user_profiles_due_idx
  ON user_profile.profiles (next_eligible_at, user_id)
  WHERE next_eligible_at IS NOT NULL;

CREATE TABLE user_profile.profile_revisions (
  user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  revision BIGINT NOT NULL CHECK (revision > 0),
  document JSONB NOT NULL CHECK (
    jsonb_typeof(document) = 'object'
    AND jsonb_typeof(document -> 'claims') = 'array'
  ),
  observed_through_event_id BIGINT NOT NULL CHECK (
    observed_through_event_id > 0
  ),
  route_key VARCHAR(300) NOT NULL CHECK (char_length(btrim(route_key)) > 0),
  prompt_version INTEGER NOT NULL CHECK (prompt_version > 0),
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, revision)
);

ALTER TABLE user_profile.profiles
  ADD CONSTRAINT user_profiles_current_revision_fk
  FOREIGN KEY (user_id, current_revision)
  REFERENCES user_profile.profile_revisions(user_id, revision)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE user_profile.dreams (
  dream_id UUID PRIMARY KEY,
  user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  base_revision BIGINT NOT NULL CHECK (base_revision >= 0),
  base_observed_through_event_id BIGINT NOT NULL CHECK (
    base_observed_through_event_id >= 0
  ),
  through_event_id BIGINT NOT NULL CHECK (
    through_event_id > base_observed_through_event_id
  ),
  source_count INTEGER NOT NULL CHECK (source_count > 0),
  metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
  status TEXT NOT NULL CHECK (
    status IN ('pending','retry_wait','processing','completed','failed_final')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  result_patch JSONB CHECK (
    result_patch IS NULL OR jsonb_typeof(result_patch) = 'object'
  ),
  route_key VARCHAR(300),
  last_error VARCHAR(1000),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  CONSTRAINT user_profile_dreams_ready_ck CHECK (
    (status IN ('pending','retry_wait')) = (next_attempt_at IS NOT NULL)
  ),
  CONSTRAINT user_profile_dreams_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  ),
  CONSTRAINT user_profile_dreams_terminal_ck CHECK (
    (status IN ('completed','failed_final')) = (completed_at IS NOT NULL)
  ),
  CONSTRAINT user_profile_dreams_result_ck CHECK (
    status <> 'completed' OR (
      result_patch IS NOT NULL AND route_key IS NOT NULL
    )
  ),
  CONSTRAINT user_profile_dreams_time_ck CHECK (
    updated_at >= created_at
    AND (completed_at IS NULL OR completed_at >= created_at)
  ),
  UNIQUE (
    user_id,
    base_revision,
    base_observed_through_event_id,
    through_event_id
  )
);

CREATE UNIQUE INDEX user_profile_dreams_active_user_uq
  ON user_profile.dreams (user_id)
  WHERE status IN ('pending','retry_wait','processing');

CREATE INDEX user_profile_dreams_ready_idx
  ON user_profile.dreams (next_attempt_at, dream_id)
  WHERE status IN ('pending','retry_wait');

CREATE INDEX user_profile_dreams_expired_lease_idx
  ON user_profile.dreams (lease_expires_at, dream_id)
  WHERE status = 'processing';

CREATE TABLE user_profile.dream_sources (
  dream_id UUID NOT NULL
    REFERENCES user_profile.dreams(dream_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  event_id BIGINT NOT NULL
    REFERENCES user_profile.evidence_events(event_id) ON DELETE CASCADE,
  PRIMARY KEY (dream_id, ordinal),
  UNIQUE (dream_id, event_id)
);

DROP TABLE assistant.ai_user_affection;

-- migrate:down

CREATE TABLE assistant.ai_user_affection (
  user_id BIGINT NOT NULL PRIMARY KEY,
  affection INT NOT NULL DEFAULT 0,
  impression VARCHAR(500),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_ai_user_affection_user
    FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE CASCADE
);

DROP SCHEMA user_profile CASCADE;
