-- migrate:up

-- A chance round first commits a server seed, then atomically takes a Free
-- wallet stake and settles.  The private seed is intentionally retained only
-- while status = committed; the settlement transition clears it after the
-- publicly verifiable proof has disclosed the seed.
CREATE SCHEMA IF NOT EXISTS chance;

CREATE TABLE chance.rounds (
  round_id UUID PRIMARY KEY,
  owner_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal', 'group')),
  scope_id BIGINT NOT NULL,
  topic_id BIGINT NULL CHECK (topic_id IS NULL OR topic_id > 0),
  ruleset JSONB NOT NULL CHECK (jsonb_typeof(ruleset) = 'object'),
  ruleset_fingerprint CHAR(64) NOT NULL CHECK (
    ruleset_fingerprint ~ '^[0-9a-f]{64}$'
  ),
  rule_code VARCHAR(64) NOT NULL CHECK (
    rule_code ~ '^[a-z][a-z0-9_.-]{0,63}$'
  ),
  stake BIGINT NOT NULL CHECK (stake > 0),
  nonce BIGINT NOT NULL CHECK (nonce >= 0),
  commitment CHAR(64) NOT NULL CHECK (commitment ~ '^[0-9a-f]{64}$'),
  server_seed BYTEA NULL CHECK (
    server_seed IS NULL OR octet_length(server_seed) >= 16
  ),
  client_seed TEXT NULL CHECK (
    client_seed IS NULL OR octet_length(client_seed) BETWEEN 1 AND 512
  ),
  status TEXT NOT NULL CHECK (status IN ('committed', 'settled')),
  outcome_code VARCHAR(64) NULL CHECK (
    outcome_code IS NULL OR outcome_code ~ '^[a-z][a-z0-9_.-]{0,63}$'
  ),
  payout BIGINT NULL CHECK (payout IS NULL OR payout > 0),
  proof JSONB NULL CHECK (proof IS NULL OR jsonb_typeof(proof) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  settled_at TIMESTAMPTZ NULL,
  CONSTRAINT chance_rounds_scope_shape_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0 AND topic_id IS NULL AND owner_id = scope_id)
    OR (scope_kind = 'group' AND scope_id <> 0)
  ),
  CONSTRAINT chance_rounds_state_shape_ck CHECK (
    (
      status = 'committed'
      AND server_seed IS NOT NULL
      AND client_seed IS NULL
      AND outcome_code IS NULL
      AND payout IS NULL
      AND proof IS NULL
      AND settled_at IS NULL
    )
    OR (
      status = 'settled'
      AND server_seed IS NULL
      AND client_seed IS NOT NULL
      AND outcome_code IS NOT NULL
      AND proof IS NOT NULL
      AND settled_at IS NOT NULL
    )
  ),
  CONSTRAINT chance_rounds_timeline_ck CHECK (
    settled_at IS NULL OR settled_at >= committed_at
  )
);

CREATE INDEX chance_rounds_owner_created_idx
  ON chance.rounds (owner_id, committed_at DESC, round_id DESC);
CREATE INDEX chance_rounds_scope_status_created_idx
  ON chance.rounds (scope_kind, scope_id, topic_id, status, committed_at DESC, round_id DESC);

-- The application adapter is the only runtime component that reads
-- server_seed, and only through a locked settlement path.  This trigger makes
-- the committed-to-settled transition one-way: it freezes the published
-- commitment and ruleset, clears the private seed, and prevents a settled
-- proof from being replaced later.
CREATE FUNCTION chance.enforce_round_lifecycle()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'chance round % is durable and cannot be deleted', OLD.round_id
      USING ERRCODE = '55000';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'committed' THEN
      RAISE EXCEPTION 'chance round % must first be inserted as committed', NEW.round_id
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status <> 'committed' OR NEW.status <> 'settled' THEN
    RAISE EXCEPTION 'chance round % permits only committed-to-settled transition', OLD.round_id
      USING ERRCODE = '55000';
  END IF;
  IF NEW.round_id IS DISTINCT FROM OLD.round_id
    OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
    OR NEW.scope_kind IS DISTINCT FROM OLD.scope_kind
    OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
    OR NEW.topic_id IS DISTINCT FROM OLD.topic_id
    OR NEW.ruleset IS DISTINCT FROM OLD.ruleset
    OR NEW.ruleset_fingerprint IS DISTINCT FROM OLD.ruleset_fingerprint
    OR NEW.rule_code IS DISTINCT FROM OLD.rule_code
    OR NEW.stake IS DISTINCT FROM OLD.stake
    OR NEW.nonce IS DISTINCT FROM OLD.nonce
    OR NEW.commitment IS DISTINCT FROM OLD.commitment
    OR NEW.committed_at IS DISTINCT FROM OLD.committed_at THEN
    RAISE EXCEPTION 'chance round % immutable commitment fields changed', OLD.round_id
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER chance_rounds_one_way_settlement_tr
BEFORE INSERT OR UPDATE OR DELETE ON chance.rounds
FOR EACH ROW EXECUTE FUNCTION chance.enforce_round_lifecycle();

CREATE TABLE chance.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  operation_kind VARCHAR(80) NOT NULL CHECK (
    operation_kind IN ('chance.commit', 'chance.bind_and_settle')
  ),
  actor_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  request_fingerprint CHAR(64) NOT NULL CHECK (
    request_fingerprint ~ '^[0-9a-f]{64}$'
  ),
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX chance_operation_receipts_actor_created_idx
  ON chance.operation_receipts (actor_id, created_at DESC, idempotency_key DESC);

CREATE FUNCTION chance.forbid_receipt_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'chance operation receipts are append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER chance_operation_receipts_append_only_tr
BEFORE UPDATE OR DELETE ON chance.operation_receipts
FOR EACH ROW EXECUTE FUNCTION chance.forbid_receipt_mutation();

-- migrate:down

DROP SCHEMA IF EXISTS chance CASCADE;
