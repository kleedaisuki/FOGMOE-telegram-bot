-- migrate:up

-- A normal renewal placement locks billing.subscriptions first.  The partial
-- unique index below is still required because direct SQL, a future ingress,
-- or an accidental lock-order regression must not create two outstanding
-- payment obligations for the same subscription.
--
-- Keep the preflight and index creation inside one writer-excluding lock: a
-- migration must fail loudly for historical duplicates rather than leave an
-- ambiguous existing order to win arbitrarily.
-- @brief 阻止并发写入并审计历史续费重复 / Exclude concurrent writers and audit historical renewal duplicates.
LOCK TABLE billing.orders IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM billing.orders AS orders
    WHERE orders.renewal_subscription_id IS NOT NULL
      AND orders.status IN ('awaiting_payment', 'paid', 'refund_pending')
    GROUP BY orders.renewal_subscription_id
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION
      'cannot install billing renewal-order integrity: duplicate open renewal orders exist'
      USING ERRCODE = '23505';
  END IF;
END;
$$;

-- @brief 每个订阅最多一笔待付款、待履约或退款结算中的续费订单 / Permit at most one awaiting-payment, paid, or refund-pending renewal order per subscription.
CREATE UNIQUE INDEX billing_orders_one_open_renewal_uq
  ON billing.orders (renewal_subscription_id)
  WHERE renewal_subscription_id IS NOT NULL
    AND status IN ('awaiting_payment', 'paid', 'refund_pending');

-- migrate:down

-- @brief 移除续费未终态唯一索引 / Remove the open-renewal unique index.
DROP INDEX IF EXISTS billing.billing_orders_one_open_renewal_uq;
