-- migrate:up

-- Personal RPG is deliberately a private, non-monetary progression context.
-- `characters` is the mutable aggregate header; material rows are a small
-- replaceable projection under that header lock.  Exploration, crafting and
-- operation receipts are durable audit facts and are append-only.
CREATE SCHEMA IF NOT EXISTS personal_rpg;

CREATE TABLE personal_rpg.characters (
  user_id BIGINT PRIMARY KEY
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  name VARCHAR(40) NOT NULL
    CHECK (char_length(btrim(name)) BETWEEN 1 AND 40),
  experience BIGINT NOT NULL DEFAULT 0 CHECK (experience >= 0),
  last_exploration_day DATE NULL,
  character_version BIGINT NOT NULL DEFAULT 0 CHECK (character_version >= 0),
  profile_version BIGINT NOT NULL DEFAULT 0 CHECK (profile_version >= 0),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE personal_rpg.materials (
  user_id BIGINT NOT NULL
    REFERENCES personal_rpg.characters(user_id) ON DELETE RESTRICT,
  material_kind TEXT NOT NULL CHECK (material_kind IN (
    'fiber', 'herb', 'stone', 'ore', 'shell', 'algae'
  )),
  quantity BIGINT NOT NULL CHECK (quantity > 0),
  PRIMARY KEY (user_id, material_kind)
);

CREATE TABLE personal_rpg.explorations (
  exploration_id UUID PRIMARY KEY,
  user_id BIGINT NOT NULL
    REFERENCES personal_rpg.characters(user_id) ON DELETE RESTRICT,
  exploration_day DATE NOT NULL,
  route TEXT NOT NULL CHECK (route IN ('woodland', 'quarry', 'shore')),
  explored_at TIMESTAMPTZ NOT NULL,
  experience_reward BIGINT NOT NULL CHECK (experience_reward > 0),
  material_rewards JSONB NOT NULL
    CHECK (jsonb_typeof(material_rewards) = 'object'),
  audit_digest CHAR(64) NOT NULL CHECK (audit_digest ~ '^[0-9a-f]{64}$'),
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT personal_rpg_explorations_user_day_uq UNIQUE (user_id, exploration_day),
  CONSTRAINT personal_rpg_explorations_utc_day_ck CHECK (
    (explored_at AT TIME ZONE 'UTC')::date = exploration_day
  )
);

CREATE INDEX personal_rpg_explorations_user_created_idx
  ON personal_rpg.explorations (user_id, exploration_day DESC, exploration_id DESC);

CREATE TABLE personal_rpg.collections (
  user_id BIGINT NOT NULL
    REFERENCES personal_rpg.characters(user_id) ON DELETE RESTRICT,
  collectible_kind TEXT NOT NULL CHECK (collectible_kind IN (
    'herbal_lantern', 'rune_charm', 'tidal_mobile'
  )),
  recipe_code TEXT NOT NULL CHECK (recipe_code IN (
    'herbal_lantern', 'rune_charm', 'tidal_mobile'
  )),
  craft_id UUID NOT NULL UNIQUE,
  crafted_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, collectible_kind),
  CONSTRAINT personal_rpg_collections_recipe_output_ck CHECK (
    collectible_kind = recipe_code
  )
);

CREATE INDEX personal_rpg_collections_user_crafted_idx
  ON personal_rpg.collections (user_id, crafted_at DESC, collectible_kind);

CREATE TABLE personal_rpg.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  operation_kind VARCHAR(80) NOT NULL CHECK (operation_kind IN (
    'personal_rpg.create_character',
    'personal_rpg.explore_daily',
    'personal_rpg.craft_recipe'
  )),
  -- A NOT_REGISTERED result is itself idempotent.  Therefore this actor is a
  -- stable personal-scope identifier rather than an FK: the first command may
  -- legitimately arrive before identity.users has a row.
  actor_id BIGINT NOT NULL CHECK (actor_id > 0),
  request_fingerprint JSONB NOT NULL
    CHECK (jsonb_typeof(request_fingerprint) = 'object'),
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX personal_rpg_operation_receipts_actor_created_idx
  ON personal_rpg.operation_receipts (actor_id, created_at DESC, idempotency_key DESC);

CREATE FUNCTION personal_rpg.forbid_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'personal_rpg.% is append-only; write a new fact instead', TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER personal_rpg_explorations_append_only_tr
BEFORE UPDATE OR DELETE ON personal_rpg.explorations
FOR EACH ROW EXECUTE FUNCTION personal_rpg.forbid_append_only_mutation();

CREATE TRIGGER personal_rpg_collections_append_only_tr
BEFORE UPDATE OR DELETE ON personal_rpg.collections
FOR EACH ROW EXECUTE FUNCTION personal_rpg.forbid_append_only_mutation();

CREATE TRIGGER personal_rpg_operation_receipts_append_only_tr
BEFORE UPDATE OR DELETE ON personal_rpg.operation_receipts
FOR EACH ROW EXECUTE FUNCTION personal_rpg.forbid_append_only_mutation();

-- migrate:down

DROP SCHEMA IF EXISTS personal_rpg CASCADE;
