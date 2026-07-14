-- migrate:up

-- PostgreSQL runs BEFORE INSERT triggers before it decides whether an INSERT
-- will take ON CONFLICT DO UPDATE.  The old projection implementation used
-- NEW.delta as that speculative INSERT's balance, so a legitimate debit from
-- an existing positive wallet was rejected as a negative *new* balance before
-- the UPDATE branch could add it to the existing value.
--
-- First ensure a zero projection row with a targeted conflict no-op, then
-- apply the delta by a separate UPDATE.  The update holds the balance row
-- lock, preserves additive concurrent writes, and lets the nonnegative
-- trigger inspect the final balance rather than a provisional debit.
-- /**
--  * @brief 通过零初始化和独立更新应用账本余额投影 / Apply a ledger balance projection through zero initialization and a separate update.
--  * @param NEW 新增账本过账行 / Newly inserted ledger posting.
--  * @return NEW，保留新增过账行 / NEW to retain the inserted posting.
--  * @note 零初始化的冲突 no-op 处理并发缺失投影；独立 UPDATE 保留行锁和最终非负校验 /
--  *       The zero-initialization conflict no-op handles a concurrently missing projection; the separate UPDATE preserves row locking and final nonnegative validation.
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

  -- Never put NEW.delta in an INSERT proposal: BEFORE INSERT validation runs
  -- before ON CONFLICT arbitration, including when a positive row already
  -- exists and the intended operation is an ordinary debit.
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

-- migrate:down

-- Restore the 0055 function exactly for revision semantics.  This definition
-- retains the historical UPSERT behavior and is intentionally not a safe
-- production rollback target after 0056 has repaired debit posting.
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

  INSERT INTO bank.account_balances (
    account_key, balance, version, updated_at
  ) VALUES (
    NEW.account_key, NEW.delta, 0, CURRENT_TIMESTAMP
  )
  ON CONFLICT (account_key) DO UPDATE
  SET balance = bank.account_balances.balance + EXCLUDED.balance,
      version = bank.account_balances.version + 1,
      updated_at = CURRENT_TIMESTAMP;

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
  '@brief 在账本过账触发器内更新余额投影 / Update balance projections inside the ledger-posting trigger.
@return NEW，保留账本过账行 / NEW to retain the ledger posting.
@note 此定义只用于严格 revision 降级；生产环境应保持 0056 的安全投影实现 /
      This definition exists only for strict revision downgrade; production must retain 0056''s safe projection implementation.';
