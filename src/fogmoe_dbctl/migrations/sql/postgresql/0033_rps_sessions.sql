-- migrate:up

-- RPS keeps its callback version in a first-class row because entry charging,
-- choices, payouts and refunds must commit with the matching aggregate state.
-- Telegram message addresses are durable metadata, never transaction inputs.
CREATE TABLE game.rps_sessions (
  game_id TEXT PRIMARY KEY CHECK (game_id ~ '^[A-Za-z0-9_-]{8,24}$'),
  status TEXT NOT NULL CHECK (
    status IN ('waiting', 'choosing', 'finished', 'cancelled', 'expired', 'invalidated')
  ),
  version BIGINT NOT NULL CHECK (version >= 0),
  player_one_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  player_two_id BIGINT
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  state JSONB NOT NULL CHECK (
    jsonb_typeof(state) = 'object'
    AND state -> 'schema' = '1'::JSONB
  ),
  delivery JSONB CHECK (
    delivery IS NULL OR jsonb_typeof(delivery) = 'object'
  ),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  terminal_at TIMESTAMPTZ,
  CONSTRAINT rps_sessions_players_ck CHECK (
    player_two_id IS NULL OR player_two_id <> player_one_id
  ),
  CONSTRAINT rps_sessions_time_ck CHECK (
    expires_at > created_at
    AND updated_at >= created_at
    AND (terminal_at IS NULL OR terminal_at >= created_at)
  ),
  CONSTRAINT rps_sessions_terminal_ck CHECK (
    (status IN ('waiting', 'choosing')) = (terminal_at IS NULL)
  ),
  CONSTRAINT rps_sessions_phase_ck CHECK (
    (status = 'waiting' AND version = 0 AND player_two_id IS NULL)
    OR (status = 'choosing' AND version >= 1 AND player_two_id IS NOT NULL)
    OR (status = 'finished' AND version >= 2 AND player_two_id IS NOT NULL)
    OR (status = 'cancelled')
    OR (status IN ('expired', 'invalidated') AND player_two_id IS NULL)
  )
);

CREATE UNIQUE INDEX uq_rps_single_waiting_room
  ON game.rps_sessions (status)
  WHERE status = 'waiting';

CREATE INDEX idx_rps_sessions_due
  ON game.rps_sessions (expires_at, game_id)
  WHERE status IN ('waiting', 'choosing');

CREATE INDEX idx_rps_sessions_terminal
  ON game.rps_sessions (terminal_at DESC, game_id)
  WHERE terminal_at IS NOT NULL;

-- A separate slot table makes cross-role uniqueness exact: one player cannot
-- be player_one in one active game and player_two in another.
CREATE TABLE game.rps_player_slots (
  user_id BIGINT PRIMARY KEY
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  game_id TEXT NOT NULL
    REFERENCES game.rps_sessions(game_id) ON DELETE CASCADE
);

CREATE INDEX idx_rps_player_slots_game
  ON game.rps_player_slots (game_id);

-- migrate:down

DROP TABLE game.rps_player_slots;
DROP TABLE game.rps_sessions;
