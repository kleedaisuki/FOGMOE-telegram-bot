-- migrate:up

-- Durable game sessions replace process-local dictionaries and timers.  A
-- partial unique index makes each active scope an invariant rather than a
-- best-effort Python lock: one global gamble pool and one Sic Bo flow per user.
CREATE TABLE game.game_sessions (
  session_id UUID PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('gamble', 'sicbo')),
  scope_key TEXT NOT NULL
    CHECK (char_length(btrim(scope_key)) BETWEEN 1 AND 255),
  owner_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  chat_id BIGINT NOT NULL CHECK (chat_id <> 0),
  message_id BIGINT NOT NULL CHECK (message_id > 0),
  state JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
  status TEXT NOT NULL CHECK (
    status IN ('active', 'settled', 'cancelled', 'expired')
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  settled_at TIMESTAMPTZ,
  notification_enqueued_at TIMESTAMPTZ,
  CONSTRAINT game_sessions_time_order_ck CHECK (
    expires_at > created_at AND updated_at >= created_at
  ),
  CONSTRAINT game_sessions_settlement_shape_ck CHECK (
    (status = 'settled') = (settled_at IS NOT NULL)
  ),
  CONSTRAINT game_sessions_notification_shape_ck CHECK (
    notification_enqueued_at IS NULL OR status = 'settled'
  )
);

CREATE UNIQUE INDEX uq_game_sessions_active_scope
  ON game.game_sessions (kind, scope_key)
  WHERE status = 'active';

CREATE INDEX idx_game_sessions_due
  ON game.game_sessions (expires_at, session_id)
  WHERE status = 'active';

CREATE INDEX idx_game_sessions_unnotified_gamble
  ON game.game_sessions (settled_at, session_id)
  WHERE kind = 'gamble'
    AND status = 'settled'
    AND notification_enqueued_at IS NULL;

-- Every Telegram retry returns the first committed semantic result.  The
-- operation CHECK is deliberately closed so a new mutation cannot silently
-- start writing an undocumented receipt schema.
CREATE TABLE game.game_receipts (
  idempotency_key TEXT PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 255),
  operation TEXT NOT NULL CHECK (
    operation IN (
      'gamble.open',
      'gamble.bet',
      'sicbo.open',
      'sicbo.select',
      'sicbo.cancel',
      'sicbo.play',
      'omikuji.draw',
      'rpg.allow',
      'rpg.heal',
      'rpg.monster_battle',
      'rpg.player_battle',
      'rpg.equip',
      'rpg.unequip',
      'rpg.use_item',
      'rpg.add_item',
      'rpg.remove_item'
    )
  ),
  user_id BIGINT NOT NULL CHECK (user_id > 0),
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT game_receipts_result_schema_ck CHECK (
    jsonb_typeof(result) = 'object'
    AND result ? 'schema'
    AND result ? 'code'
    AND result -> 'schema' = '1'::JSONB
    AND jsonb_typeof(result -> 'code') = 'string'
  )
);

-- The old in-memory battle cooldowns vanished on restart and differed across
-- replicas.  This row is also a stable transaction gate for one player's
-- battle initiation stream.
CREATE TABLE game.rpg_battle_cooldowns (
  user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE CASCADE,
  battle_kind TEXT NOT NULL CHECK (battle_kind IN ('player', 'monster')),
  last_battle_at TIMESTAMPTZ,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  PRIMARY KEY (user_id, battle_kind)
);

-- Keep the exact pre-migration values which must be repaired below.  These
-- small audit tables make a prompt downgrade data-reversible without carrying
-- snapshots of already-valid rows.  Character/inventory restores are guarded
-- by version = 0 on downgrade, so a later legitimate game mutation is never
-- overwritten by stale migration data.
CREATE TABLE game.migration_0027_character_repairs (
  character_id INTEGER PRIMARY KEY
    REFERENCES game.rpg_characters(character_id) ON DELETE CASCADE,
  level INTEGER,
  hp INTEGER,
  max_hp INTEGER,
  atk INTEGER,
  matk INTEGER,
  def INTEGER,
  experience BIGINT,
  allow_battle BOOLEAN,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO game.migration_0027_character_repairs (
  character_id,
  level,
  hp,
  max_hp,
  atk,
  matk,
  def,
  experience,
  allow_battle
)
SELECT
  character_id,
  level,
  hp,
  max_hp,
  atk,
  matk,
  def,
  experience,
  allow_battle
FROM game.rpg_characters
WHERE level IS NULL
   OR level <= 0
   OR hp IS NULL
   OR max_hp IS NULL
   OR max_hp <= 0
   OR hp < 0
   OR hp > max_hp
   OR atk IS NULL
   OR atk < 0
   OR matk IS NULL
   OR matk < 0
   OR def IS NULL
   OR def < 0
   OR experience IS NULL
   OR experience < 0
   OR allow_battle IS NULL;

-- Normalize nullable legacy character rows before establishing aggregate
-- invariants and an optimistic-concurrency token.
UPDATE game.rpg_characters
SET
  level = GREATEST(COALESCE(level, 1), 1),
  max_hp = GREATEST(COALESCE(max_hp, 10), 1),
  atk = GREATEST(COALESCE(atk, 2), 0),
  matk = GREATEST(COALESCE(matk, 0), 0),
  def = GREATEST(COALESCE(def, 1), 0),
  experience = GREATEST(COALESCE(experience, 0), 0),
  allow_battle = COALESCE(allow_battle, TRUE);

UPDATE game.rpg_characters
SET hp = LEAST(GREATEST(COALESCE(hp, max_hp), 0), max_hp);

ALTER TABLE game.rpg_characters
  ADD COLUMN version BIGINT NOT NULL DEFAULT 0,
  ALTER COLUMN level SET NOT NULL,
  ALTER COLUMN hp SET NOT NULL,
  ALTER COLUMN max_hp SET NOT NULL,
  ALTER COLUMN atk SET NOT NULL,
  ALTER COLUMN matk SET NOT NULL,
  ALTER COLUMN def SET NOT NULL,
  ALTER COLUMN experience SET NOT NULL,
  ALTER COLUMN allow_battle SET NOT NULL,
  ADD CONSTRAINT rpg_characters_version_ck CHECK (version >= 0),
  ADD CONSTRAINT rpg_characters_stats_ck CHECK (
    level > 0
    AND max_hp > 0
    AND hp BETWEEN 0 AND max_hp
    AND atk >= 0
    AND matk >= 0
    AND def >= 0
    AND experience >= 0
  );

ALTER TABLE game.rpg_player_equipment
  ADD COLUMN version BIGINT NOT NULL DEFAULT 0,
  ADD CONSTRAINT rpg_player_equipment_version_ck CHECK (version >= 0);

-- Legacy NULL/non-positive quantities were not meaningful to the UI.  Map them
-- to the pre-existing default of one before making the invariant explicit.
CREATE TABLE game.migration_0027_inventory_repairs (
  inventory_id INTEGER PRIMARY KEY
    REFERENCES game.rpg_player_inventory(id) ON DELETE CASCADE,
  quantity INTEGER,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO game.migration_0027_inventory_repairs (inventory_id, quantity)
SELECT id, quantity
FROM game.rpg_player_inventory
WHERE quantity IS NULL OR quantity <= 0;

UPDATE game.rpg_player_inventory
SET quantity = 1
WHERE quantity IS NULL OR quantity <= 0;

ALTER TABLE game.rpg_player_inventory
  ADD COLUMN version BIGINT NOT NULL DEFAULT 0,
  ALTER COLUMN quantity SET NOT NULL,
  ADD CONSTRAINT rpg_player_inventory_quantity_ck CHECK (quantity > 0),
  ADD CONSTRAINT rpg_player_inventory_version_ck CHECK (version >= 0);

-- Existing invalid fortunes were already rendered as an error by the old bot;
-- retain the row/day while normalizing it to the neutral legacy level.
CREATE TABLE game.migration_0027_omikuji_repairs (
  user_id BIGINT NOT NULL,
  fortune_date DATE NOT NULL,
  fortune VARCHAR(10) NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, fortune_date),
  FOREIGN KEY (user_id, fortune_date)
    REFERENCES game.user_omikuji(user_id, fortune_date) ON DELETE CASCADE
);

INSERT INTO game.migration_0027_omikuji_repairs (
  user_id,
  fortune_date,
  fortune
)
SELECT user_id, fortune_date, fortune
FROM game.user_omikuji
WHERE fortune NOT IN ('大吉', '中吉', '小吉', '末吉', '凶', '大凶');

UPDATE game.user_omikuji
SET fortune = '末吉'
WHERE fortune NOT IN ('大吉', '中吉', '小吉', '末吉', '凶', '大凶');

ALTER TABLE game.user_omikuji
  ADD CONSTRAINT user_omikuji_fortune_ck CHECK (
    fortune IN ('大吉', '中吉', '小吉', '末吉', '凶', '大凶')
  );

-- migrate:down

ALTER TABLE game.user_omikuji
  DROP CONSTRAINT user_omikuji_fortune_ck;

UPDATE game.user_omikuji AS fortune
SET fortune = repair.fortune
FROM game.migration_0027_omikuji_repairs AS repair
WHERE fortune.user_id = repair.user_id
  AND fortune.fortune_date = repair.fortune_date;

DROP TABLE game.migration_0027_omikuji_repairs;

ALTER TABLE game.rpg_player_inventory
  DROP CONSTRAINT rpg_player_inventory_version_ck,
  DROP CONSTRAINT rpg_player_inventory_quantity_ck,
  ALTER COLUMN quantity DROP NOT NULL;

UPDATE game.rpg_player_inventory AS inventory
SET quantity = repair.quantity
FROM game.migration_0027_inventory_repairs AS repair
WHERE inventory.id = repair.inventory_id
  AND inventory.version = 0;

ALTER TABLE game.rpg_player_inventory
  DROP COLUMN version;

DROP TABLE game.migration_0027_inventory_repairs;

ALTER TABLE game.rpg_player_equipment
  DROP CONSTRAINT rpg_player_equipment_version_ck,
  DROP COLUMN version;

ALTER TABLE game.rpg_characters
  DROP CONSTRAINT rpg_characters_stats_ck,
  DROP CONSTRAINT rpg_characters_version_ck,
  ALTER COLUMN allow_battle DROP NOT NULL,
  ALTER COLUMN experience DROP NOT NULL,
  ALTER COLUMN def DROP NOT NULL,
  ALTER COLUMN matk DROP NOT NULL,
  ALTER COLUMN atk DROP NOT NULL,
  ALTER COLUMN max_hp DROP NOT NULL,
  ALTER COLUMN hp DROP NOT NULL,
  ALTER COLUMN level DROP NOT NULL;

UPDATE game.rpg_characters AS character
SET
  level = repair.level,
  hp = repair.hp,
  max_hp = repair.max_hp,
  atk = repair.atk,
  matk = repair.matk,
  def = repair.def,
  experience = repair.experience,
  allow_battle = repair.allow_battle
FROM game.migration_0027_character_repairs AS repair
WHERE character.character_id = repair.character_id
  AND character.version = 0;

ALTER TABLE game.rpg_characters
  DROP COLUMN version;

DROP TABLE game.migration_0027_character_repairs;

DROP TABLE game.rpg_battle_cooldowns;
DROP TABLE game.game_receipts;
DROP TABLE game.game_sessions;
