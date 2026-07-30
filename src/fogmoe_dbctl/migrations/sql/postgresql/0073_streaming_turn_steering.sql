-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- @brief revision fencing 必须与 activity claim/checkpoint 主键原子切换 /
-- Revision fencing must switch atomically with activity claims and checkpoint identities.
LOCK TABLE conversation.inference_activities,
           conversation.conversation_messages,
           assistant.tool_agent_steps
  IN SHARE ROW EXCLUSIVE MODE;

-- @brief 所有历史 activity 都是初始输入 revision；DEFAULT 也保护滚动部署中的旧 writer /
-- Every historical activity belongs to the initial-input revision; the default also protects old writers during rolling deployment.
ALTER TABLE conversation.inference_activities
  ADD COLUMN input_revision BIGINT NOT NULL DEFAULT 0
  CHECK (input_revision >= 0);

-- @brief claim generation 与 retry budget 必须是正交的 durable 计数器 /
-- Claim generation and retry budget must be orthogonal durable counters.
-- Historical attempt_count/last_error snapshots cannot reconstruct which claims reached an
-- ordinary failure outcome: dependencies and ordinary failures may be arbitrarily interleaved,
-- and processing claims have already cleared last_error.  Availability therefore wins over an
-- unverifiable guess: every existing activity receives a fresh budget.  This may permit at most
-- one configured budget window of additional retries for an active legacy activity, while never
-- prematurely terminalizing durable work or deleting business data.
ALTER TABLE conversation.inference_activities
  ADD COLUMN retry_budget_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversation.inference_activities
  ADD CONSTRAINT inference_activities_retry_budget_used_ck CHECK (
    retry_budget_used >= 0
    AND retry_budget_used <= attempt_count
    AND (
      status <> 'processing'
      OR retry_budget_used < attempt_count
    )
  );

ALTER TABLE conversation.inference_activities
  DROP CONSTRAINT inference_activities_status_check;
ALTER TABLE conversation.inference_activities
  ADD CONSTRAINT inference_activities_status_check CHECK (
    status IN (
      'pending',
      'processing',
      'steer_pending',
      'retry',
      'completed',
      'failed',
      'cancelled'
    )
  );

ALTER TABLE conversation.inference_activities
  DROP CONSTRAINT inference_activities_claimable_time_ck;
ALTER TABLE conversation.inference_activities
  ADD CONSTRAINT inference_activities_claimable_time_ck CHECK (
    (status IN ('pending', 'steer_pending', 'retry')) =
    (next_attempt_at IS NOT NULL)
  );

DROP INDEX conversation.idx_inference_activities_ready;
CREATE INDEX idx_inference_activities_ready
  ON conversation.inference_activities (next_attempt_at, activity_id)
  WHERE status IN ('pending', 'steer_pending', 'retry');

-- @brief steer_pending 是新的可领取输入代，必须进入统一 pipeline dashboard /
-- steer_pending is a new claimable input generation and must appear in the shared pipeline dashboard.
CREATE OR REPLACE VIEW observability.pipeline_health
WITH (security_barrier = true, security_invoker = false) AS
SELECT 'inbox'::TEXT AS stage,
       count(*) FILTER (WHERE status = 'pending') AS pending_count,
       count(*) FILTER (WHERE status = 'processing') AS processing_count,
       count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
       count(*) FILTER (WHERE status = 'failed_final') AS failed_final_count,
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'retry_wait')
       ) AS oldest_ready_at,
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       ) AS expired_lease_count
FROM conversation.inbound_updates
UNION ALL
SELECT 'inference',
       count(*) FILTER (WHERE status IN ('pending', 'steer_pending')),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry'),
       count(*) FILTER (WHERE status = 'failed'),
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'steer_pending', 'retry')
       ),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM conversation.inference_activities
UNION ALL
SELECT 'outbox',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'retry_wait')
       ),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM conversation.outbound_messages
UNION ALL
SELECT 'retrieval.embedding',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry_wait')),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM retrieval.passage_vectors
UNION ALL
SELECT 'user_profile.dreaming',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry_wait')),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM user_profile.dreams;

-- @brief generation 进入 checkpoint identity；历史 checkpoint 原样归入 generation zero /
-- Generation becomes part of checkpoint identity; historical checkpoints remain unchanged in generation zero.
ALTER TABLE assistant.tool_agent_steps
  ADD COLUMN generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0);
ALTER TABLE assistant.tool_agent_steps
  DROP CONSTRAINT tool_agent_steps_pkey;
ALTER TABLE assistant.tool_agent_steps
  ADD CONSTRAINT tool_agent_steps_pkey PRIMARY KEY (
    turn_id,
    generation,
    step_no
  );

-- @brief source Update 的 steer 只能形成一条 canonical user row；普通历史行不受影响 /
-- A source Update may form only one canonical steer user row; ordinary historical rows are unaffected.
CREATE UNIQUE INDEX conversation_messages_steer_source_uq
  ON conversation.conversation_messages (source_update_id)
  WHERE source_update_id IS NOT NULL
    AND content ->> 'input_kind' = 'steer';

-- migrate:down

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

LOCK TABLE conversation.inference_activities,
           conversation.conversation_messages,
           assistant.tool_agent_steps
  IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM conversation.inference_activities
    WHERE input_revision <> 0 OR status = 'steer_pending'
  ) THEN
    RAISE EXCEPTION
      'cannot downgrade 0073: inference activities contain durable steer revisions';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM assistant.tool_agent_steps
    WHERE generation <> 0
  ) THEN
    RAISE EXCEPTION
      'cannot downgrade 0073: agent checkpoints contain nonzero generations';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM conversation.inference_activities
    WHERE retry_budget_used <> 0
  ) THEN
    RAISE EXCEPTION
      'cannot downgrade 0073: inference activities contain durable retry-budget usage';
  END IF;
END
$$;

DROP INDEX conversation.conversation_messages_steer_source_uq;

ALTER TABLE assistant.tool_agent_steps
  DROP CONSTRAINT tool_agent_steps_pkey;
ALTER TABLE assistant.tool_agent_steps
  ADD CONSTRAINT tool_agent_steps_pkey PRIMARY KEY (turn_id, step_no);
ALTER TABLE assistant.tool_agent_steps
  DROP COLUMN generation;

DROP INDEX conversation.idx_inference_activities_ready;
CREATE INDEX idx_inference_activities_ready
  ON conversation.inference_activities (next_attempt_at, activity_id)
  WHERE status IN ('pending', 'retry');

-- @brief 在移除 steer 状态前精确恢复 0072 dashboard 语义 /
-- Restore the exact 0072 dashboard semantics before removing the steer state.
CREATE OR REPLACE VIEW observability.pipeline_health
WITH (security_barrier = true, security_invoker = false) AS
SELECT 'inbox'::TEXT AS stage,
       count(*) FILTER (WHERE status = 'pending') AS pending_count,
       count(*) FILTER (WHERE status = 'processing') AS processing_count,
       count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
       count(*) FILTER (WHERE status = 'failed_final') AS failed_final_count,
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'retry_wait')
       ) AS oldest_ready_at,
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       ) AS expired_lease_count
FROM conversation.inbound_updates
UNION ALL
SELECT 'inference',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry'),
       count(*) FILTER (WHERE status = 'failed'),
       min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry')),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM conversation.inference_activities
UNION ALL
SELECT 'outbox',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'retry_wait')
       ),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM conversation.outbound_messages
UNION ALL
SELECT 'retrieval.embedding',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry_wait')),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM retrieval.passage_vectors
UNION ALL
SELECT 'user_profile.dreaming',
       count(*) FILTER (WHERE status = 'pending'),
       count(*) FILTER (WHERE status = 'processing'),
       count(*) FILTER (WHERE status = 'retry_wait'),
       count(*) FILTER (WHERE status = 'failed_final'),
       min(next_attempt_at) FILTER (WHERE status IN ('pending', 'retry_wait')),
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
       )
FROM user_profile.dreams;

ALTER TABLE conversation.inference_activities
  DROP CONSTRAINT inference_activities_claimable_time_ck;
ALTER TABLE conversation.inference_activities
  ADD CONSTRAINT inference_activities_claimable_time_ck CHECK (
    (status IN ('pending', 'retry')) = (next_attempt_at IS NOT NULL)
  );

ALTER TABLE conversation.inference_activities
  DROP CONSTRAINT inference_activities_status_check;
ALTER TABLE conversation.inference_activities
  ADD CONSTRAINT inference_activities_status_check CHECK (
    status IN ('pending', 'processing', 'retry', 'completed', 'failed', 'cancelled')
  );

ALTER TABLE conversation.inference_activities
  DROP COLUMN input_revision;
ALTER TABLE conversation.inference_activities
  DROP COLUMN retry_budget_used;
