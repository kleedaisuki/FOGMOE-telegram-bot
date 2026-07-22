-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- @brief 冻结所有可能改变金额镜像或旧图片责任的 writer /
-- Freeze every writer that can change monetary mirrors or legacy picture obligations.
LOCK TABLE identity.users IN ACCESS EXCLUSIVE MODE;
LOCK TABLE bank.accounts IN ACCESS EXCLUSIVE MODE;
LOCK TABLE bank.account_balances IN ACCESS EXCLUSIVE MODE;
LOCK TABLE bank.ledger_postings IN ACCESS EXCLUSIVE MODE;
LOCK TABLE media.picture_request_receipts IN ACCESS EXCLUSIVE MODE;
LOCK TABLE media.picture_offers IN ACCESS EXCLUSIVE MODE;
LOCK TABLE conversation.outbound_messages IN SHARE MODE;

-- @brief 缺失 Bank 账户按权威读取语义视为零，任何镜像差异都拒绝迁移 /
-- Treat a missing Bank account as zero, matching authoritative read semantics, and reject every
-- mirror mismatch before deleting the redundant columns.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM identity.users AS users
    LEFT JOIN bank.account_balances AS free_balance
      ON free_balance.account_key = 'user:' || users.id::TEXT || ':free'
    LEFT JOIN bank.account_balances AS paid_balance
      ON paid_balance.account_key = 'user:' || users.id::TEXT || ':paid'
    WHERE users.coins IS DISTINCT FROM COALESCE(free_balance.balance, 0)
       OR users.coins_paid IS DISTINCT FROM COALESCE(paid_balance.balance, 0)
  ) THEN
    RAISE EXCEPTION
      'cannot retire identity money mirrors: identity and Bank balances differ'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- @brief 高清收费状态必须先人工完成投递或退款，迁移不猜测责任 /
-- HD charged/delivered state requires an explicit human settlement; the migration never guesses
-- whether that obligation should be delivered or refunded.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM media.picture_offers AS offer
    WHERE offer.state IN ('charged', 'delivered')
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy picture state: charged or delivered HD offers require manual audit'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM media.picture_offers AS offer
    -- hd_cost can be a quote on an unclaimed available offer; charged_user_id is the
    -- persisted evidence that the HD charge actually happened.
    WHERE offer.charged_user_id IS NOT NULL
      AND NOT (offer.state = 'refunded' AND offer.hd_refunded)
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy picture state: an HD charge lacks explicit refund evidence'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- @brief 预览责任依据 receipt 与 outbound 的事实判定，而不是依据滞后的 offer 状态名 /
-- Decide preview settlement from receipt/outbound facts rather than a possibly stale offer-state
-- label. A historical crash can leave ``preview_pending`` after the charged preview was delivered;
-- that row is fulfilled and safe to retire. A non-refunded preview without matching delivered
-- evidence remains an economic obligation and blocks the migration.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM media.picture_offers AS offer
    WHERE NOT offer.preview_refunded
      AND NOT EXISTS (
        SELECT 1
        FROM media.picture_request_receipts AS receipt
        JOIN conversation.outbound_messages AS outbound
          ON outbound.message_id = receipt.outbound_message_id
        WHERE receipt.offer_id = offer.offer_id
          AND outbound.status = 'delivered'
          AND receipt.result ? 'offer'
          AND jsonb_typeof(receipt.result -> 'cost') = 'number'
          AND (receipt.result ->> 'cost')::BIGINT = offer.preview_cost
      )
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy picture state: a preview charge is neither delivered nor refunded'
      USING ERRCODE = '23514';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM media.picture_request_receipts AS receipt
    LEFT JOIN media.picture_offers AS offer ON offer.offer_id = receipt.offer_id
    LEFT JOIN conversation.outbound_messages AS outbound
      ON outbound.message_id = receipt.outbound_message_id
    WHERE offer.offer_id IS NULL
      AND outbound.status IS DISTINCT FROM 'delivered'
  ) THEN
    RAISE EXCEPTION
      'cannot retire legacy picture receipts: an orphan receipt lacks delivered evidence'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- @brief Bank 保留唯一余额投影，不再反向写 identity /
-- Keep Bank as the sole balance projection and stop writing back into identity.
CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  -- @brief 进入账本投影前的事务本地上下文 / Transaction-local context before entering the ledger projection.
  previous_apply_context TEXT;
BEGIN
  previous_apply_context := current_setting('bank.ledger_posting_apply', TRUE);
  PERFORM set_config('bank.ledger_posting_apply', 'on', TRUE);

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

  PERFORM set_config(
    'bank.ledger_posting_apply',
    COALESCE(previous_apply_context, 'off'),
    TRUE
  );
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION bank.apply_ledger_posting_balance() IS
  '@brief 在账本过账触发器内只更新 Bank 余额投影 / Update only the Bank balance projection inside the ledger-posting trigger.
@return NEW，保留账本过账行 / NEW to retain the ledger posting.
@note identity 不再保存任何金额镜像 / Identity no longer stores any monetary mirror.';

DROP TRIGGER identity_users_money_projection_tr ON identity.users;
DROP FUNCTION bank.guard_identity_user_money_projection();

ALTER TABLE identity.users
  DROP CONSTRAINT identity_users_coins_projection_nonnegative_ck,
  DROP CONSTRAINT identity_users_coins_paid_projection_nonnegative_ck,
  DROP COLUMN coins,
  DROP COLUMN coins_paid,
  DROP COLUMN user_plan;

-- @brief 先删除引用 outbox 的 receipt，再删除无运行时 owner 的 offer /
-- Drop the outbox-referencing receipt first, then the offer with no runtime owner.
DROP TABLE media.picture_request_receipts;
DROP TABLE media.picture_offers;

-- @brief 0027 的一次性 repair 表不属于 head；IF EXISTS 同时修复 live/snapshot drift /
-- The one-shot 0027 repair table is not part of head; IF EXISTS reconciles live/snapshot drift.
DROP TABLE IF EXISTS game.migration_0027_omikuji_repairs;

-- migrate:down

DO $$
BEGIN
  RAISE EXCEPTION
    '0062_retire_identity_mirrors_and_legacy_media is irreversible: deleted identity mirrors, plan labels, and settled media records cannot be reconstructed truthfully'
    USING ERRCODE = '55000';
END;
$$;
