-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- @brief 跨 bounded context 的原始队列表只通过聚合观测读模型暴露 /
-- Raw queue tables across bounded contexts are exposed only through aggregate observability read models.
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

CREATE VIEW observability.retrieval_queue_health
WITH (security_barrier = true, security_invoker = false) AS
SELECT space.space_id,
       space.model,
       space.dimensions,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'pending'
       ) AS pending_count,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'processing'
       ) AS processing_count,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'retry_wait'
       ) AS retry_count,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'completed'
       ) AS completed_count,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'failed_final'
       ) AS failed_final_count,
       min(vector.next_attempt_at) FILTER (
         WHERE vector.status IN ('pending', 'retry_wait')
       ) AS oldest_ready_at,
       EXTRACT(EPOCH FROM (
         CURRENT_TIMESTAMP - min(vector.next_attempt_at) FILTER (
           WHERE vector.status IN ('pending', 'retry_wait')
         )
       )) AS oldest_ready_age_seconds,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'processing'
           AND vector.lease_expires_at <= CURRENT_TIMESTAMP
       ) AS expired_lease_count
FROM retrieval.embedding_spaces AS space
LEFT JOIN retrieval.passage_vectors AS vector USING (space_id)
GROUP BY space.space_id, space.model, space.dimensions;

-- @brief 既有应用 routine 与 snapshot 的无 PUBLIC 边界一致 /
-- Existing application routines converge with the snapshot's no-PUBLIC boundary.
DO $fogmoe_0064$
DECLARE
  schema_name TEXT;
BEGIN
  FOREACH schema_name IN ARRAY ARRAY[
    'identity', 'conversation', 'context_window', 'retrieval', 'user_profile',
    'assistant', 'scheduling', 'economy', 'moderation', 'crypto', 'game',
    'media', 'admin', 'observability', 'bank', 'billing', 'town', 'chance',
    'personal_rpg'
  ]
  LOOP
    EXECUTE format(
      'REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA %I FROM PUBLIC',
      schema_name
    );
  END LOOP;
END;
$fogmoe_0064$;

-- @brief SECURITY DEFINER 函数必须采用显式 allow-list / SECURITY DEFINER functions require an explicit allow-list.
REVOKE ALL PRIVILEGES
  ON FUNCTION observability.ensure_daily_partitions(DATE)
  FROM PUBLIC;

REVOKE ALL PRIVILEGES
  ON FUNCTION observability.drop_partitions_before(DATE)
  FROM PUBLIC;

GRANT EXECUTE
  ON FUNCTION observability.ensure_daily_partitions(DATE)
  TO {{ application_role }};

GRANT EXECUTE
  ON FUNCTION observability.drop_partitions_before(DATE)
  TO {{ application_role }};

-- @brief 函数的默认 PUBLIC EXECUTE 是全局默认，不能用 per-schema REVOKE 覆盖 / Default PUBLIC EXECUTE is global and cannot be overridden by a per-schema REVOKE.
ALTER DEFAULT PRIVILEGES
  REVOKE EXECUTE ON ROUTINES FROM PUBLIC;

-- migrate:down

DROP VIEW observability.retrieval_queue_health;

CREATE OR REPLACE VIEW observability.pipeline_health AS
SELECT 'inbox'::TEXT AS stage,
       count(*) FILTER (WHERE status = 'pending') AS pending_count,
       count(*) FILTER (WHERE status = 'processing') AS processing_count,
       count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
       count(*) FILTER (WHERE status = 'failed_final') AS failed_final_count,
       min(next_attempt_at) FILTER (
         WHERE status IN ('pending', 'retry_wait')
       ) AS oldest_ready_at,
       count(*) FILTER (
         WHERE status = 'processing' AND lease_expires_at < CURRENT_TIMESTAMP
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
         WHERE status = 'processing' AND lease_expires_at < CURRENT_TIMESTAMP
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
         WHERE status = 'processing' AND lease_expires_at < CURRENT_TIMESTAMP
       )
FROM conversation.outbound_messages;

ALTER VIEW observability.pipeline_health
  RESET (security_barrier, security_invoker);

ALTER DEFAULT PRIVILEGES
  GRANT EXECUTE ON ROUTINES TO PUBLIC;

REVOKE ALL PRIVILEGES
  ON FUNCTION observability.ensure_daily_partitions(DATE)
  FROM {{ application_role }};

REVOKE ALL PRIVILEGES
  ON FUNCTION observability.drop_partitions_before(DATE)
  FROM {{ application_role }};

DO $fogmoe_0064$
DECLARE
  schema_name TEXT;
BEGIN
  FOREACH schema_name IN ARRAY ARRAY[
    'identity', 'conversation', 'context_window', 'retrieval', 'user_profile',
    'assistant', 'scheduling', 'economy', 'moderation', 'crypto', 'game',
    'media', 'admin', 'observability', 'bank', 'billing', 'town', 'chance',
    'personal_rpg'
  ]
  LOOP
    EXECUTE format(
      'GRANT EXECUTE ON ALL ROUTINES IN SCHEMA %I TO PUBLIC',
      schema_name
    );
  END LOOP;
END;
$fogmoe_0064$;
