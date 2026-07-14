-- migrate:up

-- The town header is an audited read projection of the bank group-treasury
-- account.  Monetary truth remains in bank.ledger_entries/postings; all writes
-- are made in the same transaction by PostgresTownOperations.
CREATE SCHEMA IF NOT EXISTS town;

CREATE TABLE town.towns (
  group_id BIGINT PRIMARY KEY CHECK (group_id <> 0),
  title VARCHAR(120) NOT NULL
    CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
  created_at TIMESTAMPTZ NOT NULL,
  treasury_balance BIGINT NOT NULL DEFAULT 0 CHECK (treasury_balance >= 0),
  treasury_reserved BIGINT NOT NULL DEFAULT 0 CHECK (
    treasury_reserved >= 0 AND treasury_reserved <= treasury_balance
  ),
  lifetime_contributed BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_contributed >= 0),
  lifetime_settled BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_settled >= 0),
  contribution_count BIGINT NOT NULL DEFAULT 0 CHECK (contribution_count >= 0),
  prosperity BIGINT NOT NULL DEFAULT 0 CHECK (prosperity >= 0),
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT town_treasury_conservation_ck CHECK (
    lifetime_contributed = treasury_balance + lifetime_settled
  ),
  CONSTRAINT town_towns_timeline_ck CHECK (updated_at >= created_at)
);

CREATE TABLE town.projects (
  project_id UUID PRIMARY KEY,
  group_id BIGINT NOT NULL
    REFERENCES town.towns(group_id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK (kind IN (
    'community_hall', 'workshop', 'garden', 'observatory'
  )),
  title VARCHAR(120) NOT NULL
    CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
  required_amount BIGINT NOT NULL CHECK (required_amount > 0),
  created_by BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL,
  prosperity_reward BIGINT NOT NULL CHECK (prosperity_reward > 0),
  funded_amount BIGINT NOT NULL DEFAULT 0 CHECK (
    funded_amount >= 0 AND funded_amount <= required_amount
  ),
  status TEXT NOT NULL CHECK (status IN ('funding', 'ready', 'completed')),
  completed_at TIMESTAMPTZ NULL,
  settlement_ledger_entry_id UUID NULL UNIQUE
    REFERENCES bank.ledger_entries(entry_id) ON DELETE RESTRICT,
  version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT town_projects_scope_identity_uq UNIQUE (group_id, project_id),
  CONSTRAINT town_projects_state_shape_ck CHECK (
    (status = 'funding' AND funded_amount < required_amount
      AND completed_at IS NULL AND settlement_ledger_entry_id IS NULL)
    OR (status = 'ready' AND funded_amount = required_amount
      AND completed_at IS NULL AND settlement_ledger_entry_id IS NULL)
    OR (status = 'completed' AND funded_amount = required_amount
      AND completed_at IS NOT NULL AND settlement_ledger_entry_id IS NOT NULL)
  ),
  CONSTRAINT town_projects_timeline_ck CHECK (
    completed_at IS NULL OR completed_at >= created_at
  )
);

CREATE INDEX town_projects_group_created_idx
  ON town.projects (group_id, created_at ASC, project_id ASC);
CREATE INDEX town_projects_group_open_idx
  ON town.projects (group_id, status, created_at ASC, project_id ASC)
  WHERE status IN ('funding', 'ready');

CREATE TABLE town.contributions (
  contribution_id UUID PRIMARY KEY,
  group_id BIGINT NOT NULL
    REFERENCES town.towns(group_id) ON DELETE RESTRICT,
  contributor_id BIGINT NOT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  amount BIGINT NOT NULL CHECK (amount > 0),
  contributed_at TIMESTAMPTZ NOT NULL,
  ledger_entry_id UUID NOT NULL UNIQUE
    REFERENCES bank.ledger_entries(entry_id) ON DELETE RESTRICT,
  project_id UUID NULL,
  CONSTRAINT town_contributions_project_scope_fk
    FOREIGN KEY (group_id, project_id)
    REFERENCES town.projects(group_id, project_id) ON DELETE RESTRICT
);

CREATE INDEX town_contributions_group_created_idx
  ON town.contributions (group_id, contributed_at DESC, contribution_id DESC);
CREATE INDEX town_contributions_contributor_created_idx
  ON town.contributions (contributor_id, contributed_at DESC, contribution_id DESC);

CREATE TABLE town.operation_receipts (
  idempotency_key VARCHAR(200) PRIMARY KEY
    CHECK (char_length(btrim(idempotency_key)) BETWEEN 1 AND 200),
  operation_kind VARCHAR(80) NOT NULL
    CHECK (char_length(btrim(operation_kind)) BETWEEN 1 AND 80),
  actor_id BIGINT NULL
    REFERENCES identity.users(id) ON DELETE RESTRICT,
  group_id BIGINT NOT NULL CHECK (group_id <> 0)
    REFERENCES town.towns(group_id) ON DELETE RESTRICT,
  request_fingerprint JSONB NOT NULL
    CHECK (jsonb_typeof(request_fingerprint) = 'object'),
  result JSONB NOT NULL CHECK (jsonb_typeof(result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX town_operation_receipts_group_created_idx
  ON town.operation_receipts (group_id, created_at DESC, idempotency_key DESC);

CREATE FUNCTION town.forbid_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'town.% is append-only; write a compensating state transition instead', TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER town_contributions_append_only_tr
BEFORE UPDATE OR DELETE ON town.contributions
FOR EACH ROW EXECUTE FUNCTION town.forbid_append_only_mutation();

CREATE TRIGGER town_operation_receipts_append_only_tr
BEFORE UPDATE OR DELETE ON town.operation_receipts
FOR EACH ROW EXECUTE FUNCTION town.forbid_append_only_mutation();

-- migrate:down

DROP SCHEMA IF EXISTS town CASCADE;
