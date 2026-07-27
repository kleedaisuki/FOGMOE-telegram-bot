-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief 在切换 tool catalog 前冻结可恢复 Agent 状态 / Freeze recoverable Agent state before changing the tool catalog.
-- The locks close the check-then-write window inside this migration transaction.  Operators must
-- still stop every old Bot binary before deployment: locks cannot prevent an old process from
-- writing an obsolete tool call after this transaction commits.
LOCK TABLE conversation.inference_activities,
           assistant.tool_agent_steps,
           assistant.tool_effect_receipts
  IN SHARE ROW EXCLUSIVE MODE;

-- @brief 拒绝仍会加载旧 checkpoint 或再次领取旧 receipt 的工作 / Reject work that could load an old checkpoint or claim an old receipt again.
DO $fogmoe_0069_drain_legacy_python$
DECLARE
  nonterminal_inference_count BIGINT;
  replayable_legacy_receipt_count BIGINT;
BEGIN
  SELECT count(*)
  INTO nonterminal_inference_count
  FROM conversation.inference_activities
  WHERE status NOT IN ('completed', 'failed', 'cancelled');

  IF nonterminal_inference_count > 0 THEN
    RAISE EXCEPTION
      '0069 requires a drained inference queue; found % non-terminal conversation.inference_activities row(s)',
      nonterminal_inference_count
      USING ERRCODE = '55000';
  END IF;

  -- ``PostgresAssistantToolStore`` reclaims every status except ``succeeded``.  In particular,
  -- ``failed_final`` is not safe archival state for an external execution in the current store.
  SELECT count(*)
  INTO replayable_legacy_receipt_count
  FROM assistant.tool_effect_receipts
  WHERE tool_name = 'execute_python_code'
    AND status <> 'succeeded';

  IF replayable_legacy_receipt_count > 0 THEN
    RAISE EXCEPTION
      '0069 refuses legacy execute_python_code receipt(s) that could be replayed; found % non-succeeded row(s)',
      replayable_legacy_receipt_count
      USING ERRCODE = '55000';
  END IF;
END;
$fogmoe_0069_drain_legacy_python$;

-- @brief 创建独立 Workspace 边界，避免 Assistant 工具 receipt 成为 host runtime 状态镜像 /
-- Create a dedicated Workspace boundary so Assistant tool receipts cannot become a host-runtime state mirror.
CREATE SCHEMA IF NOT EXISTS workspace;
REVOKE ALL PRIVILEGES ON SCHEMA workspace FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA workspace
  REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA workspace
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA workspace
  REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA workspace
  REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC;

-- @brief 每个个人或整群只绑定一个随机、不可轮换的可恢复 runtime key /
-- Bind each personal user or whole group to one random, non-rotating recoverable runtime key.
CREATE TABLE workspace.runtimes (
  runtime_key UUID PRIMARY KEY,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('personal', 'group')),
  scope_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT workspace_runtimes_scope_uq UNIQUE (scope_kind, scope_id),
  CONSTRAINT workspace_runtimes_scope_ck CHECK (
    (scope_kind = 'personal' AND scope_id > 0)
    OR (scope_kind = 'group' AND scope_id <> 0)
  )
);

COMMENT ON TABLE workspace.runtimes IS
  'Immutable personal-or-whole-group to opaque host runtime identity mapping; host process state remains in wspctld.';
COMMENT ON COLUMN workspace.runtimes.runtime_key IS
  'Random opaque UUID used only to recover the corresponding host workspace runtime.';
COMMENT ON COLUMN workspace.runtimes.scope_kind IS
  'personal for one user; group for one whole Telegram group, never a topic.';

-- @brief runtime key 是恢复边界，不得由通用应用 DML 静默改绑 / A runtime key is a recovery boundary and must not be silently rebound by generic application DML.
CREATE FUNCTION workspace.forbid_runtime_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fogmoe_workspace_immutable$
BEGIN
  RAISE EXCEPTION
    'workspace.runtimes is immutable; create an explicit future runtime generation instead'
    USING ERRCODE = '55000';
END;
$fogmoe_workspace_immutable$;

CREATE TRIGGER workspace_runtimes_immutable_tr
BEFORE UPDATE OR DELETE ON workspace.runtimes
FOR EACH ROW EXECUTE FUNCTION workspace.forbid_runtime_mutation();

-- Successful historical ``execute_python_code`` receipts, checkpoint JSON, and projected
-- conversation messages intentionally remain immutable audit history.  They are not translated to
-- ``run_bash``: Judge0 and a persistent Workspace have different authority, filesystem, timeout,
-- and idempotency semantics.  With the inference queue drained and the old catalog entry removed,
-- no retained checkpoint can execute legacy Python again.
--
-- No direct application-role GRANT appears here.  fogmoe-dbctl's single controlled access-policy
-- convergence owns the minimal workspace schema/table grants after this revision, avoiding a
-- second, drifting authorization surface in a migration file.

-- migrate:down

DO $fogmoe_0069_irreversible$
BEGIN
  RAISE EXCEPTION
    '0069 is irreversible: dropping workspace.runtimes would orphan recoverable host overlays and runtime identities'
    USING ERRCODE = '0A000';
END;
$fogmoe_0069_irreversible$;
