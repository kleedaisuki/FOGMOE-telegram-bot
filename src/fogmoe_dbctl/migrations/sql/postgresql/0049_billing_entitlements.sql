-- migrate:up

-- Billing is deliberately independent of bank.  It records provider-native
-- payment units and entitlement rights only; no table contains a token amount,
-- exchange rate, or a payment-to-token conversion.
CREATE SCHEMA IF NOT EXISTS billing;

CREATE TABLE billing.products (
  product_id UUID PRIMARY KEY,
  code VARCHAR(96) NOT NULL UNIQUE
    CHECK (code ~ '^[a-z][a-z0-9_.-]{0,95}$'),
  display_name VARCHAR(120) NOT NULL
    CHECK (char_length(btrim(display_name)) BETWEEN 1 AND 120),
  kind TEXT NOT NULL CHECK (kind IN ('one_time', 'subscription')),
  status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
  description TEXT NOT NULL DEFAULT '' CHECK (char_length(description) <= 2000),
  created_at TIMESTAMPTZ NOT NULL,
  retired_at TIMESTAMPTZ NULL,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_products_id_kind_uq UNIQUE (product_id, kind),
  CONSTRAINT billing_products_retirement_shape_ck CHECK (
    (status = 'active' AND retired_at IS NULL)
    OR (status = 'retired' AND retired_at IS NOT NULL AND retired_at >= created_at)
  )
);

CREATE TABLE billing.offers (
  offer_id UUID PRIMARY KEY,
  product_id UUID NOT NULL
    REFERENCES billing.products(product_id) ON DELETE RESTRICT,
  product_kind TEXT NOT NULL CHECK (product_kind IN ('one_time', 'subscription')),
  currency VARCHAR(16) NOT NULL CHECK (currency ~ '^[A-Z0-9]{3,16}$'),
  price_units BIGINT NOT NULL CHECK (price_units > 0),
  entitlement_codes JSONB NOT NULL CHECK (
    jsonb_typeof(entitlement_codes) = 'array'
    AND jsonb_array_length(entitlement_codes) > 0
  ),
  created_at TIMESTAMPTZ NOT NULL,
  subscription_period_seconds BIGINT NULL CHECK (
    subscription_period_seconds IS NULL OR subscription_period_seconds > 0
  ),
  available_from TIMESTAMPTZ NULL,
  available_until TIMESTAMPTZ NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
  retired_at TIMESTAMPTZ NULL,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_offers_kind_period_shape_ck CHECK (
    (product_kind = 'one_time' AND subscription_period_seconds IS NULL)
    OR (product_kind = 'subscription' AND subscription_period_seconds IS NOT NULL)
  ),
  CONSTRAINT billing_offers_availability_shape_ck CHECK (
    available_from IS NULL OR available_until IS NULL OR available_until > available_from
  ),
  CONSTRAINT billing_offers_retirement_shape_ck CHECK (
    (status = 'active' AND retired_at IS NULL)
    OR (status = 'retired' AND retired_at IS NOT NULL AND retired_at >= created_at)
  ),
  CONSTRAINT billing_offers_product_kind_fk FOREIGN KEY (product_id, product_kind)
    REFERENCES billing.products(product_id, kind) ON DELETE RESTRICT,
  CONSTRAINT billing_offers_frozen_snapshot_uq UNIQUE (
    offer_id, product_id, product_kind, currency, price_units
  )
);

CREATE INDEX billing_offers_product_available_idx
  ON billing.offers (product_id, status, available_from, available_until, offer_id);

CREATE TABLE billing.orders (
  order_id UUID PRIMARY KEY,
  buyer_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  product_id UUID NOT NULL
    REFERENCES billing.products(product_id) ON DELETE RESTRICT,
  offer_id UUID NOT NULL
    REFERENCES billing.offers(offer_id) ON DELETE RESTRICT,
  renewal_subscription_id UUID NULL,
  product_kind TEXT NOT NULL CHECK (product_kind IN ('one_time', 'subscription')),
  currency VARCHAR(16) NOT NULL CHECK (currency ~ '^[A-Z0-9]{3,16}$'),
  price_units BIGINT NOT NULL CHECK (price_units > 0),
  status TEXT NOT NULL CHECK (status IN (
    'awaiting_payment', 'paid', 'fulfilled', 'cancelled', 'refund_pending',
    'refunded', 'chargeback'
  )),
  created_at TIMESTAMPTZ NOT NULL,
  payment_provider TEXT NULL CHECK (
    payment_provider IS NULL OR payment_provider IN (
      'telegram_stars', 'external', 'backoffice'
    )
  ),
  provider_payment_id VARCHAR(256) NULL CHECK (
    provider_payment_id IS NULL OR char_length(btrim(provider_payment_id)) BETWEEN 1 AND 256
  ),
  paid_at TIMESTAMPTZ NULL,
  fulfilled_at TIMESTAMPTZ NULL,
  refund_requested_at TIMESTAMPTZ NULL,
  refunded_at TIMESTAMPTZ NULL,
  cancelled_at TIMESTAMPTZ NULL,
  chargeback_at TIMESTAMPTZ NULL,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_orders_payment_pair_ck CHECK (
    (payment_provider IS NULL) = (provider_payment_id IS NULL)
  ),
  CONSTRAINT billing_orders_renewal_kind_ck CHECK (
    renewal_subscription_id IS NULL OR product_kind = 'subscription'
  ),
  CONSTRAINT billing_orders_state_shape_ck CHECK (
    (
      status = 'awaiting_payment'
      AND payment_provider IS NULL AND paid_at IS NULL AND fulfilled_at IS NULL
      AND refund_requested_at IS NULL AND refunded_at IS NULL
      AND cancelled_at IS NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'paid'
      AND payment_provider IS NOT NULL AND paid_at IS NOT NULL
      AND fulfilled_at IS NULL AND refund_requested_at IS NULL AND refunded_at IS NULL
      AND cancelled_at IS NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'fulfilled'
      AND payment_provider IS NOT NULL AND paid_at IS NOT NULL AND fulfilled_at IS NOT NULL
      AND refunded_at IS NULL AND cancelled_at IS NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'refund_pending'
      AND payment_provider IS NOT NULL AND paid_at IS NOT NULL AND fulfilled_at IS NOT NULL
      AND refund_requested_at IS NOT NULL AND refunded_at IS NULL
      AND cancelled_at IS NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'refunded'
      AND payment_provider IS NOT NULL AND paid_at IS NOT NULL AND fulfilled_at IS NOT NULL
      AND refund_requested_at IS NOT NULL AND refunded_at IS NOT NULL
      AND cancelled_at IS NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'cancelled'
      AND payment_provider IS NULL AND paid_at IS NULL AND fulfilled_at IS NULL
      AND refund_requested_at IS NULL AND refunded_at IS NULL
      AND cancelled_at IS NOT NULL AND chargeback_at IS NULL
    )
    OR (
      status = 'chargeback'
      AND payment_provider IS NOT NULL AND paid_at IS NOT NULL
      AND cancelled_at IS NULL AND chargeback_at IS NOT NULL
    )
  ),
  CONSTRAINT billing_orders_timeline_ck CHECK (
    (paid_at IS NULL OR paid_at >= created_at)
    AND (fulfilled_at IS NULL OR fulfilled_at >= paid_at)
    AND (refund_requested_at IS NULL OR refund_requested_at >= fulfilled_at)
    AND (refunded_at IS NULL OR refunded_at >= refund_requested_at)
    AND (cancelled_at IS NULL OR cancelled_at >= created_at)
    AND (chargeback_at IS NULL OR chargeback_at >= paid_at)
  ),
  CONSTRAINT billing_orders_offer_snapshot_fk FOREIGN KEY (
    offer_id, product_id, product_kind, currency, price_units
  ) REFERENCES billing.offers (
    offer_id, product_id, product_kind, currency, price_units
  )
);

CREATE INDEX billing_orders_buyer_created_idx
  ON billing.orders (buyer_id, created_at DESC, order_id DESC);
CREATE INDEX billing_orders_status_created_idx
  ON billing.orders (status, created_at, order_id);
CREATE INDEX billing_orders_renewal_subscription_idx
  ON billing.orders (renewal_subscription_id)
  WHERE renewal_subscription_id IS NOT NULL;

CREATE TABLE billing.refunds (
  refund_id UUID PRIMARY KEY,
  order_id UUID NOT NULL
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  requester_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  currency VARCHAR(16) NOT NULL CHECK (currency ~ '^[A-Z0-9]{3,16}$'),
  amount_units BIGINT NOT NULL CHECK (amount_units > 0),
  reason TEXT NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 1 AND 1000),
  status TEXT NOT NULL CHECK (status IN (
    'requested', 'approved', 'rejected', 'succeeded', 'failed', 'cancelled'
  )),
  requested_at TIMESTAMPTZ NOT NULL,
  reviewer_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  reviewed_at TIMESTAMPTZ NULL,
  review_note TEXT NULL CHECK (
    review_note IS NULL OR char_length(btrim(review_note)) BETWEEN 1 AND 1000
  ),
  settlement_provider TEXT NULL CHECK (
    settlement_provider IS NULL OR settlement_provider IN (
      'telegram_stars', 'external', 'backoffice'
    )
  ),
  provider_settlement_id VARCHAR(256) NULL CHECK (
    provider_settlement_id IS NULL
    OR char_length(btrim(provider_settlement_id)) BETWEEN 1 AND 256
  ),
  settled_at TIMESTAMPTZ NULL,
  cancelled_at TIMESTAMPTZ NULL,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_refunds_reviewer_not_requester_ck CHECK (
    reviewer_id IS NULL OR reviewer_id <> requester_id
  ),
  CONSTRAINT billing_refunds_settlement_pair_ck CHECK (
    (settlement_provider IS NULL) = (provider_settlement_id IS NULL)
  ),
  CONSTRAINT billing_refunds_timeline_ck CHECK (
    (reviewed_at IS NULL OR reviewed_at >= requested_at)
    AND (settled_at IS NULL OR (reviewed_at IS NOT NULL AND settled_at >= reviewed_at))
    AND (cancelled_at IS NULL OR cancelled_at >= requested_at)
  ),
  CONSTRAINT billing_refunds_state_shape_ck CHECK (
    (
      status = 'requested'
      AND reviewer_id IS NULL AND reviewed_at IS NULL AND settled_at IS NULL
      AND settlement_provider IS NULL AND cancelled_at IS NULL
    )
    OR (
      status IN ('approved', 'rejected')
      AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL AND settled_at IS NULL
      AND settlement_provider IS NULL AND cancelled_at IS NULL
    )
    OR (
      status IN ('succeeded', 'failed')
      AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL AND settled_at IS NOT NULL
      AND settlement_provider IS NOT NULL AND cancelled_at IS NULL
    )
    OR (
      status = 'cancelled'
      AND reviewer_id IS NULL AND reviewed_at IS NULL AND settled_at IS NULL
      AND settlement_provider IS NULL AND cancelled_at IS NOT NULL
    )
  )
);

CREATE UNIQUE INDEX billing_refunds_one_open_order_uq
  ON billing.refunds (order_id)
  WHERE status IN ('requested', 'approved');
CREATE INDEX billing_refunds_requester_created_idx
  ON billing.refunds (requester_id, requested_at DESC, refund_id DESC);

CREATE TABLE billing.payment_events (
  event_id UUID PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN (
    'telegram_stars', 'external', 'backoffice'
  )),
  provider_event_id VARCHAR(256) NOT NULL
    CHECK (char_length(btrim(provider_event_id)) BETWEEN 1 AND 256),
  provider_payment_id VARCHAR(256) NOT NULL
    CHECK (char_length(btrim(provider_payment_id)) BETWEEN 1 AND 256),
  order_id UUID NOT NULL
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  refund_id UUID NULL
    REFERENCES billing.refunds(refund_id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL CHECK (event_kind IN (
    'payment_succeeded', 'payment_failed', 'refund_succeeded', 'refund_failed',
    'chargeback_opened'
  )),
  currency VARCHAR(16) NOT NULL CHECK (currency ~ '^[A-Z0-9]{3,16}$'),
  amount_units BIGINT NOT NULL CHECK (amount_units > 0),
  occurred_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_payment_events_refund_link_ck CHECK (
    (event_kind IN ('refund_succeeded', 'refund_failed')) = (refund_id IS NOT NULL)
  ),
  CONSTRAINT billing_payment_events_provider_event_uq UNIQUE (provider, provider_event_id)
);

CREATE INDEX billing_payment_events_order_occurred_idx
  ON billing.payment_events (order_id, occurred_at, event_id);
CREATE INDEX billing_payment_events_refund_occurred_idx
  ON billing.payment_events (refund_id, occurred_at, event_id)
  WHERE refund_id IS NOT NULL;

CREATE TABLE billing.fulfillments (
  fulfillment_id UUID PRIMARY KEY,
  order_id UUID NOT NULL UNIQUE
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  operator_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  fulfilled_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX billing_fulfillments_operator_time_idx
  ON billing.fulfillments (operator_id, fulfilled_at DESC, fulfillment_id DESC);

CREATE TABLE billing.entitlement_grants (
  grant_id UUID PRIMARY KEY,
  code VARCHAR(96) NOT NULL CHECK (code ~ '^[a-z][a-z0-9_.-]{0,95}$'),
  scope TEXT NOT NULL CHECK (scope IN ('user', 'group')),
  subject_id BIGINT NOT NULL,
  source_order_id UUID NOT NULL
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  fulfillment_id UUID NOT NULL
    REFERENCES billing.fulfillments(fulfillment_id) ON DELETE RESTRICT,
  starts_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
  ended_at TIMESTAMPTZ NULL,
  revocation_reason TEXT NULL CHECK (
    revocation_reason IS NULL
    OR char_length(btrim(revocation_reason)) BETWEEN 1 AND 1000
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_entitlement_grants_subject_shape_ck CHECK (
    (scope = 'user' AND subject_id > 0)
    OR (scope = 'group' AND subject_id <> 0)
  ),
  CONSTRAINT billing_entitlement_grants_time_ck CHECK (
    (expires_at IS NULL OR expires_at > starts_at)
    AND (ended_at IS NULL OR ended_at >= starts_at)
  ),
  CONSTRAINT billing_entitlement_grants_state_shape_ck CHECK (
    (status = 'active' AND ended_at IS NULL AND revocation_reason IS NULL)
    OR (
      status = 'expired' AND expires_at IS NOT NULL AND ended_at = expires_at
      AND revocation_reason IS NULL
    )
    OR (
      status = 'revoked' AND ended_at IS NOT NULL AND revocation_reason IS NOT NULL
    )
  ),
  CONSTRAINT billing_entitlement_grants_source_code_uq UNIQUE (
    source_order_id, code, scope, subject_id
  )
);

CREATE INDEX billing_entitlement_grants_active_subject_idx
  ON billing.entitlement_grants (scope, subject_id, expires_at, grant_id)
  WHERE status = 'active';
CREATE INDEX billing_entitlement_grants_source_order_idx
  ON billing.entitlement_grants (source_order_id, grant_id);

CREATE TABLE billing.subscriptions (
  subscription_id UUID PRIMARY KEY,
  owner_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  product_id UUID NOT NULL
    REFERENCES billing.products(product_id) ON DELETE RESTRICT,
  offer_id UUID NOT NULL
    REFERENCES billing.offers(offer_id) ON DELETE RESTRICT,
  source_order_id UUID NOT NULL UNIQUE
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  current_order_id UUID NOT NULL UNIQUE
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  period_starts_at TIMESTAMPTZ NOT NULL,
  period_ends_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'cancelled', 'expired', 'revoked')),
  cancellation_requested_at TIMESTAMPTZ NULL,
  ended_at TIMESTAMPTZ NULL,
  revocation_reason TEXT NULL CHECK (
    revocation_reason IS NULL
    OR char_length(btrim(revocation_reason)) BETWEEN 1 AND 1000
  ),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT billing_subscriptions_period_ck CHECK (period_ends_at > period_starts_at),
  CONSTRAINT billing_subscriptions_cancellation_time_ck CHECK (
    cancellation_requested_at IS NULL
    OR cancellation_requested_at >= period_starts_at
       AND cancellation_requested_at < period_ends_at
  ),
  CONSTRAINT billing_subscriptions_end_time_ck CHECK (
    ended_at IS NULL OR ended_at >= period_starts_at
  ),
  CONSTRAINT billing_subscriptions_state_shape_ck CHECK (
    (status = 'active' AND ended_at IS NULL AND revocation_reason IS NULL)
    OR (
      status = 'cancelled' AND cancellation_requested_at IS NOT NULL
      AND ended_at = period_ends_at AND revocation_reason IS NULL
    )
    OR (
      status = 'expired' AND cancellation_requested_at IS NULL
      AND ended_at = period_ends_at AND revocation_reason IS NULL
    )
    OR (
      status = 'revoked' AND ended_at IS NOT NULL AND revocation_reason IS NOT NULL
    )
  )
);

CREATE INDEX billing_subscriptions_owner_active_idx
  ON billing.subscriptions (owner_id, period_ends_at, subscription_id)
  WHERE status = 'active';

ALTER TABLE billing.orders
  ADD CONSTRAINT billing_orders_renewal_subscription_fk
  FOREIGN KEY (renewal_subscription_id)
  REFERENCES billing.subscriptions(subscription_id) ON DELETE RESTRICT;

CREATE TABLE billing.subscription_periods (
  subscription_id UUID NOT NULL
    REFERENCES billing.subscriptions(subscription_id) ON DELETE RESTRICT,
  order_id UUID NOT NULL UNIQUE
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  period_starts_at TIMESTAMPTZ NOT NULL,
  period_ends_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (subscription_id, period_starts_at),
  CONSTRAINT billing_subscription_periods_time_ck CHECK (period_ends_at > period_starts_at)
);

CREATE TABLE billing.subscription_entitlement_grants (
  subscription_id UUID NOT NULL
    REFERENCES billing.subscriptions(subscription_id) ON DELETE RESTRICT,
  source_order_id UUID NOT NULL
    REFERENCES billing.orders(order_id) ON DELETE RESTRICT,
  grant_id UUID NOT NULL
    REFERENCES billing.entitlement_grants(grant_id) ON DELETE RESTRICT,
  attached_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subscription_id, grant_id),
  CONSTRAINT billing_subscription_entitlement_grants_source_uq UNIQUE (
    subscription_id, source_order_id, grant_id
  )
);

CREATE INDEX billing_subscription_entitlement_current_idx
  ON billing.subscription_entitlement_grants (subscription_id, source_order_id, grant_id);

CREATE TABLE billing.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  operation_kind VARCHAR(80) NOT NULL
    CHECK (char_length(btrim(operation_kind)) BETWEEN 1 AND 80),
  actor_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX billing_operation_receipts_actor_created_idx
  ON billing.operation_receipts (actor_id, created_at DESC)
  WHERE actor_id IS NOT NULL;

CREATE FUNCTION billing.forbid_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'billing.% is append-only; write a compensating state transition instead', TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER billing_payment_events_append_only_tr
BEFORE UPDATE OR DELETE ON billing.payment_events
FOR EACH ROW EXECUTE FUNCTION billing.forbid_append_only_mutation();

CREATE TRIGGER billing_fulfillments_append_only_tr
BEFORE UPDATE OR DELETE ON billing.fulfillments
FOR EACH ROW EXECUTE FUNCTION billing.forbid_append_only_mutation();

CREATE TRIGGER billing_subscription_periods_append_only_tr
BEFORE UPDATE OR DELETE ON billing.subscription_periods
FOR EACH ROW EXECUTE FUNCTION billing.forbid_append_only_mutation();

CREATE TRIGGER billing_subscription_grants_append_only_tr
BEFORE UPDATE OR DELETE ON billing.subscription_entitlement_grants
FOR EACH ROW EXECUTE FUNCTION billing.forbid_append_only_mutation();

CREATE TRIGGER billing_operation_receipts_append_only_tr
BEFORE UPDATE OR DELETE ON billing.operation_receipts
FOR EACH ROW EXECUTE FUNCTION billing.forbid_append_only_mutation();

-- migrate:down

DROP SCHEMA billing CASCADE;
