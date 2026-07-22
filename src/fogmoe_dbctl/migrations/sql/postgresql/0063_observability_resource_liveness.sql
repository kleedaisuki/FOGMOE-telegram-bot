-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- @brief 先添加可空列，避免带 default 的表重写 / Add a nullable column first to avoid a default-driven table rewrite.
ALTER TABLE observability.resources
  ADD COLUMN last_seen_at TIMESTAMPTZ;

-- @brief 每类分区信号仅扫描一次，再按资源聚合 / Scan each partitioned signal family once, then aggregate per resource.
WITH signal_observations AS (
  SELECT
    resource_id,
    MAX(GREATEST(occurred_at, observed_at)) AS observed_at
  FROM observability.log_records
  GROUP BY resource_id

  UNION ALL

  SELECT
    resource_id,
    MAX(GREATEST(started_at, ended_at)) AS observed_at
  FROM observability.spans
  GROUP BY resource_id

  UNION ALL

  SELECT
    resource_id,
    MAX(observed_at) AS observed_at
  FROM observability.metric_points
  GROUP BY resource_id
), latest_signal AS (
  SELECT resource_id, MAX(observed_at) AS observed_at
  FROM signal_observations
  GROUP BY resource_id
)
UPDATE observability.resources AS resource
SET last_seen_at = GREATEST(
  resource.started_at,
  COALESCE(resource.stopped_at, resource.started_at),
  latest_signal.observed_at
)
FROM latest_signal
WHERE latest_signal.resource_id = resource.resource_id;

-- @brief 无信号资源仍保留可证明的起止时间 / Preserve provable lifecycle time for resources without signals.
UPDATE observability.resources
SET last_seen_at = GREATEST(
  started_at,
  COALESCE(stopped_at, started_at)
)
WHERE last_seen_at IS NULL;

-- @brief 先验证存量数据再收紧 NOT NULL / Validate existing data before tightening NOT NULL.
ALTER TABLE observability.resources
  ADD CONSTRAINT observability_resources_liveness_ck CHECK (
    last_seen_at IS NOT NULL
    AND last_seen_at >= started_at
    AND (stopped_at IS NULL OR last_seen_at >= stopped_at)
  ) NOT VALID;

ALTER TABLE observability.resources
  VALIDATE CONSTRAINT observability_resources_liveness_ck;

ALTER TABLE observability.resources
  ALTER COLUMN last_seen_at SET NOT NULL;

-- migrate:down

ALTER TABLE observability.resources
  DROP CONSTRAINT observability_resources_liveness_ck,
  DROP COLUMN last_seen_at;
