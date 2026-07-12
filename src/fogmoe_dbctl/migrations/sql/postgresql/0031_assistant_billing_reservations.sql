-- migrate:up

-- A positive-cost Assistant Turn owns exactly one transactional billing fact.
-- Native rows retain the exact free/paid split so release never guesses which
-- balance bucket to compensate.  Legacy eager-charge rows deliberately leave
-- that unknowable split NULL and are imported only as SETTLED.
CREATE TABLE assistant.billing_reservations (
  turn_id UUID PRIMARY KEY
    REFERENCES conversation.conversation_turns(turn_id) ON DELETE RESTRICT,
  user_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  cost SMALLINT NOT NULL CHECK (cost > 0),
  free_reserved SMALLINT,
  paid_reserved SMALLINT,
  pool_contribution NUMERIC(20, 2) NOT NULL
    CHECK (pool_contribution > 0),
  status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
  reserved_at TIMESTAMPTZ NOT NULL,
  settled_at TIMESTAMPTZ,
  released_at TIMESTAMPTZ,
  legacy_eager BOOLEAN NOT NULL DEFAULT FALSE,
  CONSTRAINT assistant_billing_native_split_ck CHECK (
    (
      NOT legacy_eager
      AND free_reserved IS NOT NULL
      AND paid_reserved IS NOT NULL
      AND free_reserved >= 0
      AND paid_reserved >= 0
      AND free_reserved + paid_reserved = cost
    ) OR (
      legacy_eager
      AND free_reserved IS NULL
      AND paid_reserved IS NULL
      AND status = 'settled'
    )
  ),
  CONSTRAINT assistant_billing_terminal_shape_ck CHECK (
    (status = 'reserved' AND settled_at IS NULL AND released_at IS NULL)
    OR (status = 'settled' AND settled_at IS NOT NULL AND released_at IS NULL)
    OR (status = 'released' AND settled_at IS NULL AND released_at IS NOT NULL)
  ),
  CONSTRAINT assistant_billing_time_order_ck CHECK (
    (settled_at IS NULL OR settled_at >= reserved_at)
    AND (released_at IS NULL OR released_at >= reserved_at)
  )
);

CREATE INDEX assistant_billing_reservations_user_reserved_idx
  ON assistant.billing_reservations (user_id, reserved_at, turn_id)
  WHERE status = 'reserved';

-- Before 0031 the charge and pool credit happened during acceptance.  Import
-- those rows as terminal SETTLED facts without appending another pool posting.
-- Their historical free/paid split cannot be reconstructed truthfully, hence
-- legacy_eager and NULL split columns rather than fabricated audit data.
WITH legacy_messages AS (
  SELECT
    message.turn_id,
    (message.content #>> '{user,user_id}')::BIGINT AS user_id,
    CASE
      WHEN message.content ? 'coin_cost'
        THEN (message.content ->> 'coin_cost')::SMALLINT
      WHEN message.content -> 'media' IS NOT NULL
        AND message.content -> 'media' <> 'null'::JSONB
        THEN 5::SMALLINT
      WHEN char_length(message.content ->> 'text') > 2000 THEN 5::SMALLINT
      WHEN char_length(message.content ->> 'text') > 1000 THEN 4::SMALLINT
      WHEN char_length(message.content ->> 'text') > 500 THEN 3::SMALLINT
      WHEN char_length(message.content ->> 'text') > 100 THEN 2::SMALLINT
      ELSE 1::SMALLINT
    END AS cost,
    message.created_at
  FROM conversation.conversation_messages AS message
  WHERE message.role = 'user'
    AND message.turn_id IS NOT NULL
    AND message.content #>> '{user,user_id}' ~ '^[1-9][0-9]*$'
    AND message.content ->> 'content_kind' <> 'scheduled_prompt'
), billable AS (
  SELECT * FROM legacy_messages WHERE cost > 0
)
INSERT INTO assistant.billing_reservations (
  turn_id,
  user_id,
  cost,
  free_reserved,
  paid_reserved,
  pool_contribution,
  status,
  reserved_at,
  settled_at,
  released_at,
  legacy_eager
)
SELECT
  turn_id,
  user_id,
  cost,
  NULL,
  NULL,
  (cost::NUMERIC * 0.20)::NUMERIC(20, 2),
  'settled',
  created_at,
  created_at,
  NULL,
  TRUE
FROM billable
ON CONFLICT (turn_id) DO NOTHING;

-- migrate:down

-- A RESERVED row has already deducted its exact account buckets but has not
-- credited the pool.  Downgrade materializes that missing credit under the same
-- stable key, thereby translating it to the pre-0031 eager-charge model before
-- the state table disappears.  RELEASED rows are terminal compensated facts;
-- clawing their refunds back would be destructive and can fail after later
-- spending, so downgrade intentionally preserves those completed outcomes.
INSERT INTO economy.stake_pool_postings (pool_id, idempotency_key, delta)
SELECT
  1,
  'assistant-billing:settle:' || turn_id::TEXT,
  pool_contribution
FROM assistant.billing_reservations
WHERE status = 'reserved'
ON CONFLICT (idempotency_key) DO NOTHING;

DROP TABLE assistant.billing_reservations;
