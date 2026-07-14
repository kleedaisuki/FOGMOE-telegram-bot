-- migrate:up

-- The invariant checks deliberately run before constraints are installed.  A
-- broken historical projection must be repaired explicitly, rather than being
-- silently frozen behind new write guards.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM billing.payment_events AS payment_event
    WHERE payment_event.event_kind = 'payment_succeeded'
    GROUP BY payment_event.provider, payment_event.provider_payment_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'cannot install payment success uniqueness: duplicate provider payment references exist'
      USING ERRCODE = '23505';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM bank.ledger_entries AS ledger_entry
    LEFT JOIN bank.ledger_postings AS ledger_posting
      ON ledger_posting.entry_id = ledger_entry.entry_id
    GROUP BY ledger_entry.entry_id
    HAVING count(ledger_posting.line_no) < 2
      OR COALESCE(sum(ledger_posting.delta), 0) <> 0
  ) THEN
    RAISE EXCEPTION
      'cannot install bank entry completion guard: unbalanced historical entries exist'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM bank.accounts AS account
    LEFT JOIN bank.account_balances AS account_balance
      ON account_balance.account_key = account.account_key
    LEFT JOIN bank.ledger_postings AS ledger_posting
      ON ledger_posting.account_key = account.account_key
    GROUP BY account.account_key, account_balance.balance
    HAVING account_balance.balance IS NULL
      OR account_balance.balance <> COALESCE(sum(ledger_posting.delta), 0)
  ) THEN
    RAISE EXCEPTION
      'cannot install bank projection guard: account balance differs from ledger'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- /**
--  * @brief 为旧 Billing 回执补齐不可伪造的请求指纹列 / Add the request-fingerprint column to legacy Billing receipts.
--  * @note 全零值只表示 0053 前的不可验证历史回执；新适配器必须将其视为冲突而非可重放结果 /
--  *       The all-zero value marks unverifiable pre-0053 receipts only; the new adapter must treat it as a conflict, not a replayable result.
--  */
ALTER TABLE billing.operation_receipts
  ADD COLUMN request_fingerprint CHAR(64);

-- The table is append-only in normal operation.  This one migration-owned
-- backfill is the narrow exception needed to make existing facts explicit.
ALTER TABLE billing.operation_receipts
  DISABLE TRIGGER billing_operation_receipts_append_only_tr;

UPDATE billing.operation_receipts
SET request_fingerprint = repeat('0', 64)
WHERE request_fingerprint IS NULL;

ALTER TABLE billing.operation_receipts
  ENABLE TRIGGER billing_operation_receipts_append_only_tr;

ALTER TABLE billing.operation_receipts
  ALTER COLUMN request_fingerprint SET NOT NULL;

ALTER TABLE billing.operation_receipts
  ADD CONSTRAINT billing_operation_receipts_request_fingerprint_ck CHECK (
    request_fingerprint ~ '^[0-9a-f]{64}$'
  );

-- /**
--  * @brief 为成功支付建立提供商级去重键 / Create a provider-level deduplication key for successful payments.
--  * @note 同一 provider_payment_id 只能结算一笔订单；失败或退款事件不受此部分索引约束 /
--  *       One provider_payment_id can settle only one order; failed and refund events remain outside this partial index.
--  */
CREATE UNIQUE INDEX billing_payment_events_success_payment_uq
  ON billing.payment_events (provider, provider_payment_id)
  WHERE event_kind = 'payment_succeeded';

-- /**
--  * @brief 在分录头写入时延迟验证双重记账完整性 / Defer double-entry completeness validation from ledger-entry insertion.
--  * @param NEW 新建账本分录头 / Newly inserted ledger-entry header.
--  * @return NULL，约束触发器不替换行 / NULL because a constraint trigger does not replace a row.
--  * @note 复用既有 bank.assert_ledger_entry_balanced()，以阻止提交零行或单边分录 /
--  *       Reuses bank.assert_ledger_entry_balanced() to reject zero-line and one-sided entries at commit time.
--  */
CREATE CONSTRAINT TRIGGER bank_ledger_entries_complete_ct
AFTER INSERT ON bank.ledger_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION bank.assert_ledger_entry_balanced();

-- /**
--  * @brief 在账本触发器内标记受信余额投影更新 / Mark trusted balance-projection writes inside the ledger trigger.
--  * @param NEW 新增账本过账行 / Newly inserted ledger posting.
--  * @return NEW，保留新增过账行 / NEW to retain the inserted posting.
--  * @note bank.ledger_posting_apply 是事务本地上下文，只与嵌套触发器深度共同授权余额写入 /
--  *       bank.ledger_posting_apply is transaction-local context and authorizes balance writes only together with nested trigger depth.
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

  INSERT INTO bank.account_balances (
    account_key, balance, version, updated_at
  ) VALUES (
    NEW.account_key, NEW.delta, 0, CURRENT_TIMESTAMP
  )
  ON CONFLICT (account_key) DO UPDATE
  SET balance = bank.account_balances.balance + EXCLUDED.balance,
      version = bank.account_balances.version + 1,
      updated_at = CURRENT_TIMESTAMP;

  -- Legacy integer columns remain read projections during the migration.  The
  -- separate identity.users write guard is intentionally deferred until every
  -- historical reservation/recovery path has been retired.
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
@note identity.users 投影守卫将在历史 reservation/recovery 写入路径收口后另行启用 / The identity.users projection guard is deferred until historical reservation/recovery writers are retired.';

-- /**
--  * @brief 拒绝直接改写账户元数据 / Reject direct account-metadata mutation.
--  * @param OLD 修改或删除前的账户行 / Account row before update or delete.
--  * @param NEW 修改后的账户行 / Account row after update.
--  * @return 永不返回；抛出完整性异常 / Never returns; raises an integrity exception.
--  * @note 新账户初始化仍允许 INSERT；任何属性变更必须通过显式迁移完成 /
--  *       New-account initialization still permits INSERT; attribute changes require an explicit migration.
--  */
CREATE FUNCTION bank.forbid_account_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION
    'bank.accounts is immutable after creation; use an explicit migration instead'
    USING ERRCODE = '55000';
  RETURN NULL;
END;
$$;

COMMENT ON FUNCTION bank.forbid_account_mutation() IS
  '@brief 拒绝直接更新或删除 bank.accounts / Reject direct UPDATE or DELETE of bank.accounts.
@return 永不返回；抛出 SQLSTATE 55000 / Never returns; raises SQLSTATE 55000.';

CREATE TRIGGER bank_accounts_no_direct_mutation_tr
BEFORE UPDATE OR DELETE ON bank.accounts
FOR EACH ROW EXECUTE FUNCTION bank.forbid_account_mutation();

-- /**
--  * @brief 仅允许账本过账触发器改写余额投影 / Allow balance-projection mutation only from the ledger-posting trigger.
--  * @param OLD 修改或删除前的余额行 / Balance row before update or delete.
--  * @param NEW 新增或修改后的余额行 / Balance row after insert or update.
--  * @return NEW，保留经授权的初始化或投影行 / NEW to retain an authorized initialization or projection row.
--  * @note 零余额、零版本 INSERT 用于新账户初始化；非零写入必须由嵌套的账本触发器发起 /
--  *       A zero-balance, zero-version INSERT initializes a new account; nonzero writes must originate in the nested ledger trigger.
--  */
CREATE FUNCTION bank.guard_account_balance_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.balance = 0 AND NEW.version = 0 THEN
      RETURN NEW;
    END IF;
  END IF;

  IF TG_OP IN ('INSERT', 'UPDATE')
    AND current_setting('bank.ledger_posting_apply', TRUE) = 'on'
    AND pg_trigger_depth() > 1 THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    'bank.account_balances is a ledger projection; post a balanced ledger entry instead'
    USING ERRCODE = '55000';
  RETURN NULL;
END;
$$;

COMMENT ON FUNCTION bank.guard_account_balance_mutation() IS
  '@brief 保护 bank.account_balances 免受直接写入 / Protect bank.account_balances from direct writes.
@return NEW，保留经授权的账户初始化或账本投影 / NEW to retain authorized account initialization or ledger projection.
@note 仅 bank.apply_ledger_posting_balance() 的嵌套触发器写入可修改已有余额 / Only nested writes from bank.apply_ledger_posting_balance() can modify an existing balance.';

CREATE TRIGGER bank_account_balances_authorize_mutation_tr
BEFORE INSERT OR UPDATE OR DELETE ON bank.account_balances
FOR EACH ROW EXECUTE FUNCTION bank.guard_account_balance_mutation();

-- migrate:down

DROP TRIGGER IF EXISTS bank_account_balances_authorize_mutation_tr
  ON bank.account_balances;
DROP FUNCTION IF EXISTS bank.guard_account_balance_mutation();

DROP TRIGGER IF EXISTS bank_accounts_no_direct_mutation_tr ON bank.accounts;
DROP FUNCTION IF EXISTS bank.forbid_account_mutation();

-- /**
--  * @brief 恢复 0053 前的余额投影触发器实现 / Restore the pre-0053 balance-projection trigger implementation.
--  * @param NEW 新增账本过账行 / Newly inserted ledger posting.
--  * @return NEW，保留新增过账行 / NEW to retain the inserted posting.
--  * @note 此回滚仅撤销本迁移的守卫，不回写或删除任何账本事实 /
--  *       This rollback removes only this migration's guards and never rewrites or deletes ledger facts.
--  */
CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  -- @brief 受影响账户的逻辑范围 / Logical scope of the affected account.
  affected_scope TEXT;
  -- @brief 受影响用户账户的所有者 / Owner of the affected user account.
  affected_owner_id BIGINT;
  -- @brief 受影响用户账户的钱包类别 / Token bucket of the affected user account.
  affected_bucket TEXT;
BEGIN
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
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION bank.apply_ledger_posting_balance() IS NULL;

DROP TRIGGER IF EXISTS bank_ledger_entries_complete_ct ON bank.ledger_entries;
DROP INDEX IF EXISTS billing.billing_payment_events_success_payment_uq;

ALTER TABLE billing.operation_receipts
  DROP CONSTRAINT IF EXISTS billing_operation_receipts_request_fingerprint_ck;
ALTER TABLE billing.operation_receipts
  DROP COLUMN IF EXISTS request_fingerprint;
