-- migrate:up

-- 0055 retires products that no longer have a Telegram route or a running
-- worker.  Historical deductions happened before Bank existed, therefore a
-- source row may still represent user-owned principal.  This migration refuses
-- malformed ownership/amount data instead of guessing or silently deleting it.
--
-- Acquire the cutover boundary before inspecting any source fact.  Otherwise a
-- straggling legacy worker could debit, settle, or mutate a JSON bet between
-- preflight and the Bank refund.  The narrow lock set intentionally excludes
-- the surviving Chance, Town, and personal-RPG tables.
LOCK TABLE
  identity.users,
  crypto.user_btc_predictions,
  crypto.operation_receipts,
  economy.user_stakes,
  economy.shop_pity,
  economy.stake_reward_pool,
  economy.stake_pool_postings,
  assistant.billing_reservations,
  game.game_sessions,
  game.game_receipts,
  game.rps_sessions,
  game.rps_player_slots,
  bank.accounts,
  bank.account_balances,
  bank.ledger_entries,
  bank.ledger_postings
IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM crypto.user_btc_predictions AS prediction
    LEFT JOIN identity.users AS users ON users.id = prediction.user_id
    WHERE prediction.is_completed IS DISTINCT FROM TRUE
      AND (
        prediction.user_id <= 0
        OR users.id IS NULL
        OR prediction.amount IS NULL
        OR prediction.amount <= 0
        OR prediction.request_key IS NULL
        OR char_length(btrim(prediction.request_key)) = 0
      )
  ) THEN
    RAISE EXCEPTION
      'cannot retire BTC predictions: an unsettled row lacks a safe owner, amount, or request key'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM economy.user_stakes AS stake
    LEFT JOIN identity.users AS users ON users.id = stake.user_id
    WHERE stake.user_id <= 0
      OR users.id IS NULL
      OR stake.stake_amount IS NULL
      OR stake.stake_amount <= 0
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy stakes: a principal row lacks a safe owner or positive amount'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM game.game_sessions AS session
    WHERE session.kind = 'gamble'
      AND session.status = 'active'
      AND jsonb_typeof(session.state -> 'bets') IS DISTINCT FROM 'array'
  ) THEN
    RAISE EXCEPTION
      'cannot retire active gamble: the held-bet state is not an array'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    WITH raw_bets AS (
      SELECT session.session_id, bet.value, bet.ordinal
      FROM game.game_sessions AS session
      CROSS JOIN LATERAL jsonb_array_elements(
        CASE
          WHEN jsonb_typeof(session.state -> 'bets') = 'array'
            THEN session.state -> 'bets'
          ELSE '[]'::JSONB
        END
      ) WITH ORDINALITY AS bet(value, ordinal)
      WHERE session.kind = 'gamble' AND session.status = 'active'
    ), parsed_bets AS (
      SELECT
        raw_bets.session_id,
        raw_bets.ordinal,
        CASE
          WHEN jsonb_typeof(raw_bets.value) = 'object'
            AND jsonb_typeof(raw_bets.value -> 'user_id') = 'number'
            AND COALESCE(raw_bets.value ->> 'user_id', '') ~ '^[1-9][0-9]{0,18}$'
          THEN (raw_bets.value ->> 'user_id')::NUMERIC
          ELSE NULL
        END AS user_id_value,
        CASE
          WHEN jsonb_typeof(raw_bets.value) = 'object'
            AND jsonb_typeof(raw_bets.value -> 'amount') = 'number'
            AND COALESCE(raw_bets.value ->> 'amount', '') ~ '^[1-9][0-9]{0,18}$'
          THEN (raw_bets.value ->> 'amount')::NUMERIC
          ELSE NULL
        END AS amount_value
      FROM raw_bets
    )
    SELECT 1
    FROM parsed_bets AS bet
    LEFT JOIN identity.users AS users
      ON users.id = CASE
        WHEN bet.user_id_value BETWEEN 1 AND 9223372036854775807
          THEN bet.user_id_value::BIGINT
        ELSE NULL
      END
    WHERE bet.user_id_value IS NULL
      OR bet.user_id_value > 9223372036854775807
      OR bet.amount_value IS NULL
      OR bet.amount_value > 9223372036854775807
      OR users.id IS NULL
  ) THEN
    RAISE EXCEPTION
      'cannot retire active gamble: a held bet lacks a positive existing user or BIGINT amount'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    WITH raw_bets AS (
      SELECT session.session_id, bet.value
      FROM game.game_sessions AS session
      CROSS JOIN LATERAL jsonb_array_elements(
        CASE
          WHEN jsonb_typeof(session.state -> 'bets') = 'array'
            THEN session.state -> 'bets'
          ELSE '[]'::JSONB
        END
      ) AS bet(value)
      WHERE session.kind = 'gamble' AND session.status = 'active'
    )
    SELECT 1
    FROM raw_bets
    GROUP BY session_id, value ->> 'user_id'
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'cannot retire active gamble: one session contains duplicate user bets'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM game.rps_sessions AS session
    LEFT JOIN identity.users AS first_user ON first_user.id = session.player_one_id
    LEFT JOIN identity.users AS second_user ON second_user.id = session.player_two_id
    WHERE session.status = 'choosing'
      AND (
        session.version < 1
        OR session.player_one_id <= 0
        OR session.player_two_id IS NULL
        OR session.player_two_id <= 0
        OR session.player_one_id = session.player_two_id
        OR jsonb_typeof(session.state) <> 'object'
        OR first_user.id IS NULL
        OR second_user.id IS NULL
      )
  ) THEN
    RAISE EXCEPTION
      'cannot retire active RPS: a held-entry session has invalid player or state shape'
      USING ERRCODE = '23514';
  END IF;

  -- Every remaining pool contribution must be attributable through either a
  -- terminal Assistant billing reservation or the legacy eager-charge aggregate
  -- below.  An unowned 0022 opening balance is an operator-only exception: it
  -- must be explicitly removed through dbctl before this migration runs, never
  -- silently discarded as part of the general retirement path.
  IF EXISTS (
    SELECT 1
    FROM economy.stake_pool_postings AS posting
    WHERE posting.pool_id <> 1
      OR posting.delta <= 0
      OR posting.idempotency_key NOT LIKE 'assistant-acceptance:pool:1:tx:%'
        AND posting.idempotency_key NOT LIKE 'assistant-billing:settle:%'
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy stake pool: an unallocated or unexpected posting requires manual disposition'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM economy.stake_reward_pool AS pool
    WHERE pool.balance <> 0
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy stake pool: a nonzero cached pool balance requires manual disposition'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM economy.stake_pool_postings AS posting
    WHERE posting.idempotency_key LIKE 'assistant-billing:settle:%'
      AND posting.idempotency_key !~
        '^assistant-billing:settle:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  ) OR EXISTS (
    SELECT 1
    FROM economy.stake_pool_postings AS posting
    LEFT JOIN assistant.billing_reservations AS reservation
      ON reservation.turn_id =
        CASE
          WHEN posting.idempotency_key ~
            '^assistant-billing:settle:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          THEN substring(posting.idempotency_key FROM 26)::UUID
          ELSE NULL
        END
    WHERE posting.idempotency_key LIKE 'assistant-billing:settle:%'
      AND (
        reservation.turn_id IS NULL
        OR reservation.status <> 'settled'
        OR reservation.legacy_eager
        OR reservation.pool_contribution <> posting.delta
      )
  ) OR EXISTS (
    SELECT 1
    FROM assistant.billing_reservations AS reservation
    WHERE reservation.status = 'settled'
      AND NOT reservation.legacy_eager
      AND NOT EXISTS (
        SELECT 1
        FROM economy.stake_pool_postings AS posting
        WHERE posting.idempotency_key =
          'assistant-billing:settle:' || reservation.turn_id::TEXT
          AND posting.pool_id = 1
          AND posting.delta = reservation.pool_contribution
      )
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy stake pool: a native Assistant settlement does not match its pool posting'
      USING ERRCODE = '23514';
  END IF;

  IF (
    SELECT count(*)
    FROM economy.stake_pool_postings AS posting
    WHERE posting.idempotency_key LIKE 'assistant-acceptance:pool:1:tx:%'
  ) <> (
    SELECT count(*)
    FROM assistant.billing_reservations AS reservation
    WHERE reservation.status = 'settled' AND reservation.legacy_eager
  ) OR (
    SELECT COALESCE(sum(posting.delta), 0)
    FROM economy.stake_pool_postings AS posting
    WHERE posting.idempotency_key LIKE 'assistant-acceptance:pool:1:tx:%'
  ) <> (
    SELECT COALESCE(sum(reservation.pool_contribution), 0)
    FROM assistant.billing_reservations AS reservation
    WHERE reservation.status = 'settled' AND reservation.legacy_eager
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy stake pool: eager Assistant settlements do not reconcile to pool postings'
      USING ERRCODE = '23514';
  END IF;

  -- ``system:staking_pool`` is removable only when it never became part of an
  -- immutable Bank fact. A historical posting cannot be deleted or rewritten,
  -- so a nonempty account must block retirement rather than lose audit data.
  IF EXISTS (
    SELECT 1
    FROM bank.accounts AS account
    LEFT JOIN bank.account_balances AS balance
      ON balance.account_key = account.account_key
    WHERE account.account_key = 'system:staking_pool'
      AND (
        account.account_scope <> 'system'
        OR account.owner_id IS NOT NULL
        OR account.token_bucket IS NOT NULL
        OR account.system_kind <> 'staking_pool'
        OR account.allow_negative
        OR balance.account_key IS NULL
        OR balance.balance <> 0
        OR balance.version <> 0
        OR EXISTS (
          SELECT 1
          FROM bank.ledger_postings AS posting
          WHERE posting.account_key = account.account_key
        )
      )
  ) THEN
    RAISE EXCEPTION
      'cannot retire Bank staking_pool account: it has invalid shape, balance, version, or immutable postings'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM bank.ledger_entries AS entry
    WHERE entry.reason IN ('rpg_reward', 'rpg_purchase', 'subscription_grant')
  ) THEN
    RAISE EXCEPTION
      'cannot remove dead Bank ledger reasons: immutable historical entries still use them'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- Freeze every safely attributable refund before deleting a source table.  A
-- temporary relation keeps all following balanced-entry statements driven by
-- exactly the preflighted facts, rather than re-parsing mutable JSON three
-- times.  Its primary key also makes an impossible idempotency collision fail.
CREATE TEMPORARY TABLE legacy_wager_refunds (
  idempotency_key VARCHAR(200) PRIMARY KEY,
  user_id BIGINT NOT NULL,
  amount BIGINT NOT NULL CHECK (amount > 0),
  metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata) = 'object')
) ON COMMIT DROP;

INSERT INTO legacy_wager_refunds (
  idempotency_key, user_id, amount, metadata
)
SELECT
  'migration:0055:btc-prediction-refund:' || md5(prediction.request_key),
  prediction.user_id,
  prediction.amount::BIGINT,
  jsonb_build_object(
    'migration', '0055_retire_legacy_wagers',
    'legacy_refund', TRUE,
    'legacy_source', 'crypto.user_btc_predictions',
    'legacy_source_key_md5', md5(prediction.request_key),
    'user_id', prediction.user_id,
    'principal', prediction.amount::BIGINT
  )
FROM crypto.user_btc_predictions AS prediction
WHERE prediction.is_completed IS DISTINCT FROM TRUE
UNION ALL
SELECT
  'migration:0055:stake-principal:' || stake.user_id::TEXT,
  stake.user_id,
  stake.stake_amount::BIGINT,
  jsonb_build_object(
    'migration', '0055_retire_legacy_wagers',
    'legacy_refund', TRUE,
    'legacy_source', 'economy.user_stakes',
    'legacy_source_key_md5', md5(stake.user_id::TEXT),
    'user_id', stake.user_id,
    'principal', stake.stake_amount::BIGINT
  )
FROM economy.user_stakes AS stake
UNION ALL
SELECT
  'migration:0055:gamble-refund:' || session.session_id::TEXT || ':' || bet.ordinal::TEXT,
  (bet.value ->> 'user_id')::BIGINT,
  (bet.value ->> 'amount')::BIGINT,
  jsonb_build_object(
    'migration', '0055_retire_legacy_wagers',
    'legacy_refund', TRUE,
    'legacy_source', 'game.game_sessions.gamble',
    'legacy_session_id', session.session_id::TEXT,
    'legacy_bet_ordinal', bet.ordinal,
    'legacy_source_key_md5', md5(session.session_id::TEXT || ':' || bet.ordinal::TEXT),
    'user_id', (bet.value ->> 'user_id')::BIGINT,
    'principal', (bet.value ->> 'amount')::BIGINT
  )
FROM game.game_sessions AS session
CROSS JOIN LATERAL jsonb_array_elements(
  CASE
    WHEN jsonb_typeof(session.state -> 'bets') = 'array'
      THEN session.state -> 'bets'
    ELSE '[]'::JSONB
  END
)
  WITH ORDINALITY AS bet(value, ordinal)
WHERE session.kind = 'gamble' AND session.status = 'active'
UNION ALL
SELECT
  'migration:0055:rps-principal:' || session.game_id || ':' || player.user_id::TEXT,
  player.user_id,
  1,
  jsonb_build_object(
    'migration', '0055_retire_legacy_wagers',
    'legacy_refund', TRUE,
    'legacy_source', 'game.rps_sessions',
    'legacy_game_id', session.game_id,
    'legacy_player_slot', player.slot_name,
    'legacy_source_key_md5', md5(session.game_id || ':' || player.user_id::TEXT),
    'user_id', player.user_id,
    'principal', 1
  )
FROM game.rps_sessions AS session
CROSS JOIN LATERAL (
  VALUES
    (session.player_one_id, 'player_one'::TEXT),
    (session.player_two_id, 'player_two'::TEXT)
) AS player(user_id, slot_name)
WHERE session.status = 'choosing';

-- Each settled Assistant turn funded the retired reward pool with a known
-- fraction of its integer token charge.  Return twice that original charge to
-- its recorded owner; this avoids inventing a rounding rule for the old
-- decimal pool contribution.  Any unowned opening balance has already been
-- removed by an explicit operator action before this general migration runs.
INSERT INTO legacy_wager_refunds (
  idempotency_key, user_id, amount, metadata
)
SELECT
  'migration:0055:assistant-double-refund:' || reservation.turn_id::TEXT,
  reservation.user_id,
  reservation.cost::BIGINT * 2,
  jsonb_build_object(
    'migration', '0055_retire_legacy_wagers',
    'legacy_refund', TRUE,
    'legacy_source', 'assistant.billing_reservations',
    'assistant_pool_double_refund', TRUE,
    'assistant_turn_id', reservation.turn_id::TEXT,
    'user_id', reservation.user_id,
    'original_cost', reservation.cost,
    'refund_multiplier', 2,
    'refund_amount', reservation.cost::BIGINT * 2
  )
FROM assistant.billing_reservations AS reservation
WHERE reservation.status = 'settled';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM legacy_wager_refunds AS refund
    JOIN bank.accounts AS account
      ON account.account_key = 'user:' || refund.user_id::TEXT || ':free'
    WHERE account.account_scope <> 'user'
      OR account.owner_id <> refund.user_id
      OR account.token_bucket <> 'free'
      OR account.system_kind IS NOT NULL
      OR account.allow_negative
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy wagers: an existing Free Bank wallet has an invalid shape'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- 0047/0054 normally created these wallets.  This defensive initialization
-- covers a historical user inserted by an old writer between migrations.
INSERT INTO bank.accounts (
  account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
)
SELECT
  'user:' || refund.user_id::TEXT || ':free',
  'user',
  refund.user_id,
  'free',
  NULL,
  FALSE
FROM (SELECT DISTINCT user_id FROM legacy_wager_refunds) AS refund
ON CONFLICT (account_key) DO NOTHING;

INSERT INTO bank.account_balances (account_key, balance, version, updated_at)
SELECT
  'user:' || refund.user_id::TEXT || ':free',
  0,
  0,
  CURRENT_TIMESTAMP
FROM (SELECT DISTINCT user_id FROM legacy_wager_refunds) AS refund
ON CONFLICT (account_key) DO NOTHING;

-- ``actor_id`` is deliberately NULL: these are system migration facts, not
-- user-initiated transactions.  Each beneficiary is explicit in metadata.
INSERT INTO bank.ledger_entries (
  entry_id, idempotency_key, reason, actor_id, metadata, created_at
)
SELECT
  (
    substr(md5(refund.idempotency_key), 1, 8) || '-' ||
    substr(md5(refund.idempotency_key), 9, 4) || '-' ||
    substr(md5(refund.idempotency_key), 13, 4) || '-' ||
    substr(md5(refund.idempotency_key), 17, 4) || '-' ||
    substr(md5(refund.idempotency_key), 21, 12)
  )::UUID,
  refund.idempotency_key,
  'migration_opening',
  NULL,
  refund.metadata,
  CURRENT_TIMESTAMP
FROM legacy_wager_refunds AS refund;

INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT entries.entry_id, 1, 'system:issuance', -refund.amount
FROM legacy_wager_refunds AS refund
JOIN bank.ledger_entries AS entries
  ON entries.idempotency_key = refund.idempotency_key;

INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT
  entries.entry_id,
  2,
  'user:' || refund.user_id::TEXT || ':free',
  refund.amount
FROM legacy_wager_refunds AS refund
JOIN bank.ledger_entries AS entries
  ON entries.idempotency_key = refund.idempotency_key;

-- Retire only receipt facts whose source feature disappeared.  Chart binding
-- retains the shared crypto receipt store; Omikuji retains the shared game
-- receipt store, narrowed below to its sole surviving operation.
DELETE FROM crypto.operation_receipts
WHERE operation_kind = 'prediction.create';

DELETE FROM game.game_receipts
WHERE operation LIKE 'gamble.%'
   OR operation LIKE 'sicbo.%'
   OR operation LIKE 'rpg.%';

ALTER TABLE game.game_receipts
  DROP CONSTRAINT IF EXISTS game_receipts_operation_check;
ALTER TABLE game.game_receipts
  ADD CONSTRAINT game_receipts_operation_ck CHECK (operation = 'omikuji.draw');

DROP TABLE crypto.user_btc_predictions;
DROP TABLE economy.user_stakes;
DROP TABLE economy.shop_pity;
DROP TABLE economy.stake_pool_postings;
DROP TABLE economy.stake_reward_pool;

-- Old RPG progress is intentionally not migrated into ``personal_rpg``.  The
-- two products have incompatible state, privacy scope, and audit semantics.
DROP TABLE IF EXISTS game.migration_0027_character_repairs;
DROP TABLE IF EXISTS game.migration_0027_inventory_repairs;
DROP TABLE game.rpg_battle_cooldowns;
DROP TABLE game.rpg_player_equipment_stats;
DROP TABLE game.rpg_player_equipment;
DROP TABLE game.rpg_player_inventory;
DROP TABLE game.rpg_shop;
DROP TABLE game.rpg_equipment;
DROP TABLE game.rpg_items;
DROP TABLE game.rpg_characters;

DROP TABLE game.rps_player_slots;
DROP TABLE game.rps_sessions;
DROP TABLE game.game_sessions;

-- The preflight proves this is a zero, never-posted bootstrap row. The Bank
-- guards normally make these structures immutable; an explicit migration is
-- the only narrow authority allowed to remove the dead empty account shape.
ALTER TABLE bank.account_balances
  DISABLE TRIGGER bank_account_balances_authorize_mutation_tr;
ALTER TABLE bank.accounts
  DISABLE TRIGGER bank_accounts_no_direct_mutation_tr;

DELETE FROM bank.account_balances
WHERE account_key = 'system:staking_pool';
DELETE FROM bank.accounts
WHERE account_key = 'system:staking_pool';

ALTER TABLE bank.accounts
  ENABLE TRIGGER bank_accounts_no_direct_mutation_tr;
ALTER TABLE bank.account_balances
  ENABLE TRIGGER bank_account_balances_authorize_mutation_tr;

-- 0053 deliberately defers the Bank completeness triggers until commit.  They
-- must run before altering the ledger-entry table below: PostgreSQL correctly
-- refuses ALTER TABLE while a transaction still carries pending trigger events.
-- This is also an early proof that every migration refund is balanced.
SET CONSTRAINTS ALL IMMEDIATE;

-- Drop the original unnamed checks by definition rather than assuming a
-- PostgreSQL-generated name, then reinstall the narrower live vocabulary.
DO $$
DECLARE
  constraint_name TEXT;
BEGIN
  FOR constraint_name IN
    SELECT constraint_row.conname
    FROM pg_constraint AS constraint_row
    JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'bank'
      AND relation.relname = 'accounts'
      AND constraint_row.contype = 'c'
      AND pg_get_constraintdef(constraint_row.oid) LIKE '%system_kind%'
  LOOP
    EXECUTE format('ALTER TABLE bank.accounts DROP CONSTRAINT %I', constraint_name);
  END LOOP;

  EXECUTE
    'ALTER TABLE bank.accounts ADD CONSTRAINT bank_accounts_system_kind_ck '
    || 'CHECK (system_kind IS NULL OR system_kind IN '
    || '(''issuance'', ''burn'', ''group_treasury'', ''activity_pot''))';
  EXECUTE
    'ALTER TABLE bank.accounts ADD CONSTRAINT bank_accounts_shape_ck CHECK ('
    || '(account_scope = ''user'' AND owner_id IS NOT NULL AND owner_id > 0 '
    || 'AND token_bucket IS NOT NULL AND system_kind IS NULL AND NOT allow_negative) '
    || 'OR (account_scope = ''group'' AND owner_id IS NOT NULL AND owner_id <> 0 '
    || 'AND token_bucket IS NULL AND system_kind = ''group_treasury'' '
    || 'AND NOT allow_negative) '
    || 'OR (account_scope = ''system'' AND owner_id IS NULL AND token_bucket IS NULL '
    || 'AND system_kind IS NOT NULL AND system_kind IN '
    || '(''issuance'', ''burn'', ''activity_pot'') '
    || 'AND allow_negative = (system_kind = ''issuance'')))';
END;
$$;

DO $$
DECLARE
  constraint_name TEXT;
BEGIN
  FOR constraint_name IN
    SELECT constraint_row.conname
    FROM pg_constraint AS constraint_row
    JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'bank'
      AND relation.relname = 'ledger_entries'
      AND constraint_row.contype = 'c'
      AND pg_get_constraintdef(constraint_row.oid) LIKE '%bank_issuance%'
  LOOP
    EXECUTE format(
      'ALTER TABLE bank.ledger_entries DROP CONSTRAINT %I', constraint_name
    );
  END LOOP;

  EXECUTE
    'ALTER TABLE bank.ledger_entries ADD CONSTRAINT '
    || 'bank_ledger_entries_reason_ck CHECK (reason IN '
    || '(''bank_issuance'', ''migration_opening'', ''bank_burn'', '
    || '''user_transfer'', ''group_contribution'', ''activity_stake'', '
    || '''activity_payout''))';
END;
$$;

-- migrate:down

DO $$
BEGIN
  RAISE EXCEPTION
    '0055_retire_legacy_wagers is irreversible: it replaces retired principal with immutable Bank audit entries and intentionally does not recreate legacy products'
    USING ERRCODE = '55000';
END;
$$;
