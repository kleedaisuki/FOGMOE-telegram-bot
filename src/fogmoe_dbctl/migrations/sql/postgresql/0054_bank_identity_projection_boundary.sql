-- migrate:up

-- 0054 makes the Bank the only monetary write authority.  Before installing
-- the guard, reconcile every legacy integer projection into balanced entries
-- and close any native Assistant reservation that was debited before the Bank
-- existed.  A failed preflight is deliberate: silently accepting a negative
-- or structurally incomplete historical fact would make the new invariant a
-- lie.
--
-- The lock closes the cutover race: every table that can carry a legacy
-- monetary write is held before it is inspected and remains held until the
-- projection guard is installed.  Ordinary readers are still compatible with
-- SHARE ROW EXCLUSIVE, while concurrent INSERT/UPDATE/DELETE writers wait.
LOCK TABLE
  identity.users,
  assistant.billing_reservations,
  bank.accounts,
  bank.account_balances,
  bank.ledger_entries,
  bank.ledger_postings
IN SHARE ROW EXCLUSIVE MODE;

-- 0053's UPSERT implementation can expose a negative delta to the BEFORE
-- INSERT nonnegative trigger before PostgreSQL reaches its conflict-update
-- branch.  This migration itself may append a reconciliation burn, so a fresh
-- database must install the safe projection form before that first debit.
-- 0056 repeats this repair for databases that had already reached 0055.
-- /**
--  * @brief 通过零初始化和独立更新应用账本余额投影 / Apply a ledger balance projection through zero initialization and a separate update.
--  * @param NEW 新增账本过账行 / Newly inserted ledger posting.
--  * @return NEW，保留新增过账行 / NEW to retain the inserted posting.
--  * @note 先插入零行可安全处理并发缺失投影；随后 UPDATE 才检查借记后的最终余额 /
--  *       The zero row safely handles a concurrently missing projection; the following UPDATE checks the final debited balance.
--  */
CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  -- @brief 进入账本投影前的事务本地上下文 / Transaction-local context before entering the ledger projection.
  previous_apply_context TEXT;
  -- @brief 受影响账户的逻辑范围 / Logical scope of the affected account.
  affected_scope TEXT;
  -- @brief 受影响用户账户的所有者 / Owner of the affected user account.
  affected_owner_id BIGINT;
  -- @brief 受影响用户账户的钱包类别 / Token bucket of the affected user account.
  affected_bucket TEXT;
BEGIN
  previous_apply_context := current_setting('bank.ledger_posting_apply', TRUE);
  PERFORM set_config('bank.ledger_posting_apply', 'on', TRUE);

  -- PostgreSQL runs BEFORE INSERT triggers before ON CONFLICT arbitration.
  -- Therefore the provisional row must be zero, never NEW.delta: a debit of
  -- an existing positive balance must be checked only after its row lock and
  -- arithmetic UPDATE have selected the final balance.
  INSERT INTO bank.account_balances (
    account_key, balance, version, updated_at
  ) VALUES (
    NEW.account_key, 0, 0, CURRENT_TIMESTAMP
  )
  ON CONFLICT (account_key) DO NOTHING;

  UPDATE bank.account_balances AS current_balance
  SET balance = current_balance.balance + NEW.delta,
      version = current_balance.version + 1,
      updated_at = CURRENT_TIMESTAMP
  WHERE current_balance.account_key = NEW.account_key;

  IF NOT FOUND THEN
    RAISE EXCEPTION
      'bank balance projection % was unavailable after initialization', NEW.account_key
      USING ERRCODE = '40001';
  END IF;

  SELECT account_scope, owner_id, token_bucket
    INTO affected_scope, affected_owner_id, affected_bucket
  FROM bank.accounts
  WHERE account_key = NEW.account_key;
  IF affected_scope = 'user' AND affected_owner_id IS NOT NULL THEN
    IF affected_bucket = 'free' THEN
      UPDATE identity.users AS users
      SET coins = balances.balance
      FROM bank.account_balances AS balances
      WHERE balances.account_key = NEW.account_key
        AND users.id = affected_owner_id;
    ELSIF affected_bucket = 'paid' THEN
      UPDATE identity.users AS users
      SET coins_paid = balances.balance
      FROM bank.account_balances AS balances
      WHERE balances.account_key = NEW.account_key
        AND users.id = affected_owner_id;
    END IF;
  END IF;

  PERFORM set_config(
    'bank.ledger_posting_apply',
    COALESCE(previous_apply_context, 'off'),
    TRUE
  );
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION bank.apply_ledger_posting_balance() IS
  '@brief 在账本过账触发器内安全更新余额投影 / Safely update balance projections inside the ledger-posting trigger.
@return NEW，保留账本过账行 / NEW to retain the ledger posting.
@note 先零初始化再更新，避免 PostgreSQL 在 UPSERT 冲突仲裁前把借记误判为负插入 /
      Zero initialization before UPDATE avoids PostgreSQL misclassifying a debit as a negative insert before UPSERT conflict arbitration.';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM identity.users AS users
    WHERE users.coins < 0 OR users.coins_paid < 0
  ) THEN
    RAISE EXCEPTION
      'cannot install identity monetary projection guard: a legacy balance is negative'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM identity.users AS users
    WHERE users.id <= 0 AND (users.coins <> 0 OR users.coins_paid <> 0)
  ) THEN
    RAISE EXCEPTION
      'cannot reconcile a non-positive identity user carrying monetary balance'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM assistant.billing_reservations AS reservation
    WHERE reservation.status = 'reserved'
      AND (
        reservation.user_id <= 0
        OR reservation.legacy_eager
        OR reservation.free_reserved IS NULL
        OR reservation.paid_reserved IS NULL
        OR reservation.free_reserved < 0
        OR reservation.paid_reserved < 0
        OR reservation.free_reserved + reservation.paid_reserved <> reservation.cost
      )
  ) THEN
    RAISE EXCEPTION
      'cannot close legacy Assistant reservation without an exact free/paid split'
      USING ERRCODE = '23514';
  END IF;

  -- A native reservation was historically a direct identity debit.  If a
  -- later Bank posting refreshed the same wallet projection, the remaining
  -- balance cannot prove whether the reservation is still reflected.  An
  -- automatic refund could then create money, so require a manual audit.
  -- 0047 opening entries are the one safe exception: they merely imported the
  -- pre-Bank balance and must not be treated as a later Bank mutation.
  IF EXISTS (
    SELECT 1
    FROM assistant.billing_reservations AS reservation
    JOIN LATERAL (
      VALUES
        ('free'::TEXT, reservation.free_reserved::BIGINT),
        ('paid'::TEXT, reservation.paid_reserved::BIGINT)
    ) AS reserved_bucket (bucket, amount)
      ON reserved_bucket.amount > 0
    JOIN bank.ledger_postings AS posting
      ON posting.account_key =
        'user:' || reservation.user_id::TEXT || ':' || reserved_bucket.bucket
    JOIN bank.ledger_entries AS entry
      ON entry.entry_id = posting.entry_id
    WHERE reservation.status = 'reserved'
      AND entry.idempotency_key NOT LIKE 'migration:0047:opening:%'
      AND entry.created_at >= reservation.reserved_at
  ) THEN
    RAISE EXCEPTION
      'cannot close legacy Assistant reservation after a later Bank wallet posting; manual audit is required'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- Every positive identity user must own both ledger wallets before the
-- projection is reconciled.  0047 creates these for existing users, while
-- this pass covers users inserted by a legacy writer after that migration.
INSERT INTO bank.accounts (
  account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
)
SELECT
  'user:' || users.id::TEXT || ':free',
  'user',
  users.id,
  'free',
  NULL,
  FALSE
FROM identity.users AS users
WHERE users.id > 0
ON CONFLICT (account_key) DO NOTHING;

INSERT INTO bank.accounts (
  account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
)
SELECT
  'user:' || users.id::TEXT || ':paid',
  'user',
  users.id,
  'paid',
  NULL,
  FALSE
FROM identity.users AS users
WHERE users.id > 0
ON CONFLICT (account_key) DO NOTHING;

INSERT INTO bank.account_balances (account_key, balance, version, updated_at)
SELECT accounts.account_key, 0, 0, CURRENT_TIMESTAMP
FROM bank.accounts AS accounts
WHERE accounts.account_scope = 'user'
ON CONFLICT (account_key) DO NOTHING;

-- Reconcile a possible post-0047 legacy direct write before releasing
-- historical Assistant reservations.  If the legacy projection is larger we
-- transparently import it from issuance; if it is smaller we burn the
-- corresponding bank balance.  Thus each user wallet reaches the visible
-- historical amount with an append-only, balanced explanation.
WITH legacy_projection AS (
  SELECT
    users.id AS user_id,
    'free'::TEXT AS bucket,
    users.coins::BIGINT AS projection_balance,
    balances.balance AS ledger_balance
  FROM identity.users AS users
  JOIN bank.account_balances AS balances
    ON balances.account_key = 'user:' || users.id::TEXT || ':free'
  WHERE users.id > 0
  UNION ALL
  SELECT
    users.id AS user_id,
    'paid'::TEXT AS bucket,
    users.coins_paid::BIGINT AS projection_balance,
    balances.balance AS ledger_balance
  FROM identity.users AS users
  JOIN bank.account_balances AS balances
    ON balances.account_key = 'user:' || users.id::TEXT || ':paid'
  WHERE users.id > 0
), discrepancies AS (
  SELECT
    projection.user_id,
    projection.bucket,
    projection.projection_balance - projection.ledger_balance AS delta,
    'migration:0054:identity-projection:' || projection.user_id::TEXT || ':' ||
      projection.bucket AS idempotency_key
  FROM legacy_projection AS projection
  WHERE projection.projection_balance <> projection.ledger_balance
)
INSERT INTO bank.ledger_entries (
  entry_id, idempotency_key, reason, actor_id, metadata, created_at
)
SELECT
  (
    substr(md5(discrepancy.idempotency_key), 1, 8) || '-' ||
    substr(md5(discrepancy.idempotency_key), 9, 4) || '-' ||
    substr(md5(discrepancy.idempotency_key), 13, 4) || '-' ||
    substr(md5(discrepancy.idempotency_key), 17, 4) || '-' ||
    substr(md5(discrepancy.idempotency_key), 21, 12)
  )::UUID,
  discrepancy.idempotency_key,
  'migration_opening',
  NULL,
  jsonb_build_object(
    'migration', '0054_bank_identity_projection_boundary',
    'legacy_projection_reconciliation', TRUE,
    'user_id', discrepancy.user_id,
    'bucket', discrepancy.bucket,
    'direction', CASE
      WHEN discrepancy.delta > 0 THEN 'issuance_credit'
      ELSE 'burn_debit'
    END
  ),
  CURRENT_TIMESTAMP
FROM discrepancies AS discrepancy;

WITH legacy_projection AS (
  SELECT
    users.id AS user_id,
    'free'::TEXT AS bucket,
    users.coins::BIGINT AS projection_balance,
    balances.balance AS ledger_balance
  FROM identity.users AS users
  JOIN bank.account_balances AS balances
    ON balances.account_key = 'user:' || users.id::TEXT || ':free'
  WHERE users.id > 0
  UNION ALL
  SELECT
    users.id AS user_id,
    'paid'::TEXT AS bucket,
    users.coins_paid::BIGINT AS projection_balance,
    balances.balance AS ledger_balance
  FROM identity.users AS users
  JOIN bank.account_balances AS balances
    ON balances.account_key = 'user:' || users.id::TEXT || ':paid'
  WHERE users.id > 0
), discrepancies AS (
  SELECT
    projection.user_id,
    projection.bucket,
    projection.projection_balance - projection.ledger_balance AS delta,
    'migration:0054:identity-projection:' || projection.user_id::TEXT || ':' ||
      projection.bucket AS idempotency_key
  FROM legacy_projection AS projection
  WHERE projection.projection_balance <> projection.ledger_balance
)
INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT
  entries.entry_id,
  1,
  CASE
    WHEN discrepancy.delta > 0 THEN 'system:issuance'
    ELSE 'user:' || discrepancy.user_id::TEXT || ':' || discrepancy.bucket
  END,
  -abs(discrepancy.delta)
FROM discrepancies AS discrepancy
JOIN bank.ledger_entries AS entries
  ON entries.idempotency_key = discrepancy.idempotency_key;

WITH reconciliation_entries AS (
  SELECT
    entries.entry_id,
    entries.metadata ->> 'bucket' AS bucket,
    entries.metadata ->> 'direction' AS direction,
    (entries.metadata ->> 'user_id')::BIGINT AS user_id
  FROM bank.ledger_entries AS entries
  WHERE entries.idempotency_key ~ '^migration:0054:identity-projection:[0-9]+:(free|paid)$'
), first_lines AS (
  SELECT postings.entry_id, postings.delta
  FROM bank.ledger_postings AS postings
  JOIN reconciliation_entries AS entries ON entries.entry_id = postings.entry_id
  WHERE postings.line_no = 1
)
INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT
  entries.entry_id,
  2,
  CASE
    WHEN entries.direction = 'issuance_credit' THEN
      'user:' || entries.user_id::TEXT || ':' || entries.bucket
    ELSE 'system:burn'
  END,
  -first_lines.delta
FROM reconciliation_entries AS entries
JOIN first_lines ON first_lines.entry_id = entries.entry_id;

-- A native RESERVED Assistant row has already reduced its exact legacy
-- buckets but has no bank counterparty.  Reconstruct it as a one-time refund
-- from issuance, preserving Free/Paid separation, then make the row terminal.
-- This sequence is intentionally after the projection reconciliation above.
-- The preflight accepts an automatic refund only when no later non-opening
-- Bank posting makes the historical reservation amount ambiguous.
WITH pending_refunds AS (
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'free'::TEXT AS bucket,
    reservation.free_reserved::BIGINT AS amount,
    reservation.reserved_at,
    GREATEST(CURRENT_TIMESTAMP, reservation.reserved_at) AS occurred_at,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':free'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.free_reserved > 0
  UNION ALL
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'paid'::TEXT AS bucket,
    reservation.paid_reserved::BIGINT AS amount,
    reservation.reserved_at,
    GREATEST(CURRENT_TIMESTAMP, reservation.reserved_at) AS occurred_at,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':paid'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.paid_reserved > 0
)
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
  jsonb_build_object(
    'migration', '0054_bank_identity_projection_boundary',
    'legacy_assistant_reservation_release', TRUE,
    'assistant_turn_id', refund.turn_id::TEXT,
    'user_id', refund.user_id,
    'bucket', refund.bucket,
    'amount', refund.amount,
    'reserved_at', refund.reserved_at
  ),
  refund.occurred_at
FROM pending_refunds AS refund;

WITH pending_refunds AS (
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'free'::TEXT AS bucket,
    reservation.free_reserved::BIGINT AS amount,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':free'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.free_reserved > 0
  UNION ALL
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'paid'::TEXT AS bucket,
    reservation.paid_reserved::BIGINT AS amount,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':paid'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.paid_reserved > 0
)
INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT entries.entry_id, 1, 'system:issuance', -refund.amount
FROM pending_refunds AS refund
JOIN bank.ledger_entries AS entries
  ON entries.idempotency_key = refund.idempotency_key;

WITH pending_refunds AS (
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'free'::TEXT AS bucket,
    reservation.free_reserved::BIGINT AS amount,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':free'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.free_reserved > 0
  UNION ALL
  SELECT
    reservation.turn_id,
    reservation.user_id,
    'paid'::TEXT AS bucket,
    reservation.paid_reserved::BIGINT AS amount,
    'migration:0054:assistant-release:' || reservation.turn_id::TEXT || ':paid'
      AS idempotency_key
  FROM assistant.billing_reservations AS reservation
  WHERE reservation.status = 'reserved' AND reservation.paid_reserved > 0
)
INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
SELECT
  entries.entry_id,
  2,
  'user:' || refund.user_id::TEXT || ':' || refund.bucket,
  refund.amount
FROM pending_refunds AS refund
JOIN bank.ledger_entries AS entries
  ON entries.idempotency_key = refund.idempotency_key;

UPDATE assistant.billing_reservations AS reservation
SET status = 'released',
    released_at = GREATEST(CURRENT_TIMESTAMP, reservation.reserved_at)
WHERE reservation.status = 'reserved';

-- No diverging legacy projection is allowed to survive the cutover.  The
-- final check proves the subsequent trigger will protect a true read model,
-- rather than freezing a stale second source of truth.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM identity.users AS users
    LEFT JOIN bank.accounts AS free_account
      ON free_account.account_key = 'user:' || users.id::TEXT || ':free'
    LEFT JOIN bank.account_balances AS free_balance
      ON free_balance.account_key = free_account.account_key
    LEFT JOIN bank.accounts AS paid_account
      ON paid_account.account_key = 'user:' || users.id::TEXT || ':paid'
    LEFT JOIN bank.account_balances AS paid_balance
      ON paid_balance.account_key = paid_account.account_key
    WHERE users.id > 0
      AND (
        free_account.account_key IS NULL
        OR free_account.account_scope IS DISTINCT FROM 'user'
        OR free_account.owner_id IS DISTINCT FROM users.id
        OR free_account.token_bucket IS DISTINCT FROM 'free'
        OR free_account.system_kind IS NOT NULL
        OR free_account.allow_negative IS DISTINCT FROM FALSE
        OR free_balance.account_key IS NULL
        OR paid_account.account_key IS NULL
        OR paid_account.account_scope IS DISTINCT FROM 'user'
        OR paid_account.owner_id IS DISTINCT FROM users.id
        OR paid_account.token_bucket IS DISTINCT FROM 'paid'
        OR paid_account.system_kind IS NOT NULL
        OR paid_account.allow_negative IS DISTINCT FROM FALSE
        OR paid_balance.account_key IS NULL
        OR users.coins <> free_balance.balance
        OR users.coins_paid <> paid_balance.balance
      )
  ) THEN
    RAISE EXCEPTION
      'cannot install identity monetary projection guard: legacy columns differ from bank ledger'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

ALTER TABLE identity.users
  ADD CONSTRAINT identity_users_coins_projection_nonnegative_ck CHECK (coins >= 0),
  ADD CONSTRAINT identity_users_coins_paid_projection_nonnegative_ck CHECK (
    coins_paid >= 0
  );

-- /**
--  * @brief 仅允许账本投影触发器更新旧余额镜像 / Allow legacy balance mirrors to be updated only by the ledger-projection trigger.
--  * @param OLD 更新前 identity 用户行 / Identity user row before update.
--  * @param NEW 待插入或更新的 identity 用户行 / Identity user row to insert or update.
--  * @return NEW，保留合法的零初始化或账本投影 / NEW, retaining legal zero initialization or ledger projection.
--  * @note pg_trigger_depth 与 transaction-local 标志共同防止应用 SQL 伪造投影写入 /
--  *       pg_trigger_depth and the transaction-local flag jointly prevent application SQL from forging a projection write.
--  */
CREATE FUNCTION bank.guard_identity_user_money_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.coins = 0 AND NEW.coins_paid = 0 THEN
      RETURN NEW;
    END IF;
  ELSIF TG_OP = 'UPDATE' THEN
    IF NEW.coins = OLD.coins AND NEW.coins_paid = OLD.coins_paid THEN
      RETURN NEW;
    END IF;
    IF current_setting('bank.ledger_posting_apply', TRUE) = 'on'
      AND pg_trigger_depth() > 1 THEN
      RETURN NEW;
    END IF;
  END IF;

  RAISE EXCEPTION
    'identity.users coins are bank-ledger projections; post a balanced bank entry instead'
    USING ERRCODE = '55000';
  RETURN NULL;
END;
$$;

COMMENT ON FUNCTION bank.guard_identity_user_money_projection() IS
  '@brief 守卫 identity.users 的金币投影 / Guard identity.users token projections.
@return NEW，保留零初始化或账本触发器投影 / NEW, retaining zero initialization or ledger-trigger projection.
@note 直接金币写入必须改为 bank.ledger_entries 加平衡 postings / Direct token writes must become bank.ledger_entries plus balanced postings.';

CREATE TRIGGER identity_users_money_projection_tr
BEFORE INSERT OR UPDATE OF coins, coins_paid ON identity.users
FOR EACH ROW EXECUTE FUNCTION bank.guard_identity_user_money_projection();

-- Assistant token charging is retired.  0054 has converted every previously
-- reserved row into an audited released fact, so the table remains historical
-- audit evidence only and cannot create a future direct-balance bypass.
-- /**
--  * @brief 禁止修改已退役 Assistant 金币预留 / Forbid mutation of retired Assistant token reservations.
--  * @param OLD 旧预留行 / Existing reservation row.
--  * @param NEW 待写入预留行 / Reservation row proposed for write.
--  * @return 永不返回；抛出完整性异常 / Never returns; raises an integrity exception.
--  */
CREATE FUNCTION bank.forbid_legacy_assistant_billing_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION
    'assistant token billing is retired; use entitlement Billing instead'
    USING ERRCODE = '55000';
  RETURN NULL;
END;
$$;

COMMENT ON FUNCTION bank.forbid_legacy_assistant_billing_mutation() IS
  '@brief 封存旧 Assistant 金币预留表 / Archive the retired Assistant token-reservation table.
@return 永不返回；抛出 SQLSTATE 55000 / Never returns; raises SQLSTATE 55000.';

CREATE TRIGGER assistant_billing_reservations_retired_tr
BEFORE INSERT OR UPDATE OR DELETE ON assistant.billing_reservations
FOR EACH ROW EXECUTE FUNCTION bank.forbid_legacy_assistant_billing_mutation();

-- migrate:down

DROP TRIGGER IF EXISTS assistant_billing_reservations_retired_tr
  ON assistant.billing_reservations;
DROP FUNCTION IF EXISTS bank.forbid_legacy_assistant_billing_mutation();

DROP TRIGGER IF EXISTS identity_users_money_projection_tr ON identity.users;
DROP FUNCTION IF EXISTS bank.guard_identity_user_money_projection();

ALTER TABLE identity.users
  DROP CONSTRAINT IF EXISTS identity_users_coins_projection_nonnegative_ck,
  DROP CONSTRAINT IF EXISTS identity_users_coins_paid_projection_nonnegative_ck;

-- Reconciliation entries, released Assistant facts, and their ledger effects
-- are intentionally not undone: they are immutable audit history, not a
-- reversible schema object.
