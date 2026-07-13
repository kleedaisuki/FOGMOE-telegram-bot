"""@brief PostgreSQL 只读分析 repository / PostgreSQL read-only analytics repository."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import ceil
from typing import Any, cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from fogmoe_dashboard.domain.models import (
    ErrorEvent,
    GenAiStats,
    HealthPoint,
    LogEntry,
    MetricStats,
    Overview,
    PipelineStage,
    ResourceInstance,
    RetrievalQueueStats,
    RetrievalSnapshot,
    SlowTurn,
    SpanStats,
    TimeWindow,
    TraceDetail,
    TraceLog,
    TraceSpan,
    TraceSummary,
    TurnLatencyStats,
    freeze_json_object,
)


class PostgresDashboardRepository:
    """@brief 通过只读、短超时连接池执行有界查询 / Execute bounded queries through a read-only, short-timeout pool."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 4,
        command_timeout: float = 5.0,
    ) -> None:
        """@brief 保存惰性连接配置 / Store lazy connection configuration.

        @param dsn PostgreSQL DSN / PostgreSQL DSN.
        @param pool_size 最大并行查询连接 / Maximum concurrent query connections.
        @param command_timeout 单条命令超时秒数 / Per-command timeout in seconds.
        @return None / None.
        """

        normalized = dsn.strip().replace("postgresql+asyncpg://", "postgresql://", 1)
        if not normalized:
            raise ValueError("Dashboard DSN cannot be blank")
        if isinstance(pool_size, bool) or not 1 <= pool_size <= 16:
            raise ValueError("Dashboard pool_size must be between 1 and 16")
        if command_timeout <= 0 or command_timeout > 60:
            raise ValueError("Dashboard command_timeout must be in (0, 60]")
        self._dsn = normalized
        self._pool_size = pool_size
        self._command_timeout = command_timeout
        self._pool: asyncpg.Pool | None = None

    async def overview(self, window: TimeWindow) -> Overview:
        """@brief 查询 RED 总览与 pipeline 饱和度 / Query RED overview and pipeline saturation."""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(_OVERVIEW_SQL, window.start, window.end)
            pipeline_rows = await connection.fetch(_PIPELINE_SQL)
        if row is None:
            raise RuntimeError("Overview query returned no row")
        return Overview(
            generated_at=_datetime(row["generated_at"]),
            window=window,
            spans=int(row["spans"]),
            error_spans=int(row["error_spans"]),
            traces=int(row["traces"]),
            logs=int(row["logs"]),
            error_logs=int(row["error_logs"]),
            p50_ms=_optional_float(row["p50_ms"]),
            p95_ms=_optional_float(row["p95_ms"]),
            p99_ms=_optional_float(row["p99_ms"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            tool_calls=int(row["tool_calls"]),
            pipeline=tuple(_pipeline_stage(value) for value in pipeline_rows),
        )

    async def pipeline(self) -> Sequence[PipelineStage]:
        """@brief 查询当前 pipeline 健康 / Query current pipeline health."""

        return tuple(_pipeline_stage(row) for row in await self._fetch(_PIPELINE_SQL))

    async def health_series(
        self,
        window: TimeWindow,
        *,
        buckets: int,
    ) -> Sequence[HealthPoint]:
        """@brief 查询健康趋势聚合桶 / Query health-trend aggregate buckets."""

        bucket_seconds = max(
            1,
            ceil((window.end - window.start).total_seconds() / buckets),
        )
        rows = await self._fetch(
            _HEALTH_SERIES_SQL,
            window.start,
            window.end,
            bucket_seconds,
        )
        return tuple(
            HealthPoint(
                observed_at=_datetime(row["observed_at"]),
                span_rate_per_second=float(row["span_rate_per_second"]),
                span_error_rate=float(row["span_error_rate"]),
                p95_ms=_optional_float(row["p95_ms"]),
                error_logs=int(row["error_logs"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
            )
            for row in rows
        )

    async def spans(
        self,
        window: TimeWindow,
        *,
        name: str | None,
        limit: int,
    ) -> Sequence[SpanStats]:
        """@brief 查询操作 RED 聚合 / Query operation RED aggregates."""

        rows = await self._fetch(_SPANS_SQL, window.start, window.end, name, limit)
        return tuple(
            SpanStats(
                name=str(row["span_name"]),
                kind=str(row["span_kind"]),
                calls=int(row["calls"]),
                rate_per_second=float(row["rate_per_second"]),
                errors=int(row["errors"]),
                p50_ms=float(row["p50_ms"]),
                p95_ms=float(row["p95_ms"]),
                p99_ms=float(row["p99_ms"]),
                average_ms=float(row["average_ms"]),
                maximum_ms=float(row["maximum_ms"]),
            )
            for row in rows
        )

    async def errors(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[ErrorEvent]:
        """@brief 查询 span/log 合并错误流 / Query the merged span/log error stream."""

        rows = await self._fetch(_ERRORS_SQL, window.start, window.end, limit)
        return tuple(
            ErrorEvent(
                occurred_at=_datetime(row["occurred_at"]),
                source=str(row["source"]),
                name=str(row["name"]),
                message=str(row["message"]),
                trace_id=_optional_string(row["trace_id"]),
                turn_id=_optional_uuid(row["turn_id"]),
            )
            for row in rows
        )

    async def logs(
        self,
        window: TimeWindow,
        *,
        minimum_severity: int,
        logger_name: str | None,
        limit: int,
    ) -> Sequence[LogEntry]:
        """@brief 查询过滤日志 / Query filtered logs."""

        rows = await self._fetch(
            _LOGS_SQL,
            window.start,
            window.end,
            minimum_severity,
            logger_name,
            limit,
        )
        return tuple(
            LogEntry(
                occurred_at=_datetime(row["occurred_at"]),
                severity_number=int(row["severity_number"]),
                severity_text=str(row["severity_text"]),
                logger_name=str(row["logger_name"]),
                event_name=_optional_string(row["event_name"]),
                body=str(row["body"]),
                trace_id=_optional_string(row["trace_id"]),
                span_id=_optional_string(row["span_id"]),
                turn_id=_optional_uuid(row["turn_id"]),
            )
            for row in rows
        )

    async def traces(
        self,
        window: TimeWindow,
        *,
        errors_only: bool,
        limit: int,
    ) -> Sequence[TraceSummary]:
        """@brief 查询 trace 摘要 / Query trace summaries."""

        rows = await self._fetch(
            _TRACES_SQL,
            window.start,
            window.end,
            errors_only,
            limit,
        )
        return tuple(
            TraceSummary(
                trace_id=str(row["trace_id"]),
                started_at=_datetime(row["started_at"]),
                ended_at=_datetime(row["ended_at"]),
                duration_ms=float(row["duration_ms"]),
                span_count=int(row["span_count"]),
                error_count=int(row["error_count"]),
                root_operations=tuple(str(value) for value in row["root_operations"]),
            )
            for row in rows
        )

    async def trace(self, trace_id: str) -> TraceDetail:
        """@brief 查询一个 trace 的 spans 与 logs / Query one trace's spans and logs."""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            span_rows = await connection.fetch(_TRACE_SPANS_SQL, trace_id)
            log_rows = await connection.fetch(_TRACE_LOGS_SQL, trace_id)
        return TraceDetail(
            trace_id=trace_id,
            spans=tuple(
                TraceSpan(
                    span_id=str(row["span_id"]),
                    parent_span_id=_optional_string(row["parent_span_id"]),
                    name=str(row["span_name"]),
                    kind=str(row["span_kind"]),
                    status=str(row["status_code"]),
                    started_at=_datetime(row["started_at"]),
                    ended_at=_datetime(row["ended_at"]),
                    duration_ms=float(row["duration_ms"]),
                    status_message=_optional_string(row["status_message"]),
                    attributes=freeze_json_object(row["attributes"]),
                )
                for row in span_rows
            ),
            logs=tuple(
                TraceLog(
                    occurred_at=_datetime(row["occurred_at"]),
                    span_id=_optional_string(row["span_id"]),
                    severity_text=str(row["severity_text"]),
                    logger_name=str(row["logger_name"]),
                    event_name=_optional_string(row["event_name"]),
                    body=str(row["body"]),
                )
                for row in log_rows
            ),
        )

    async def metrics(
        self,
        window: TimeWindow,
        *,
        name: str | None,
        limit: int,
    ) -> Sequence[MetricStats]:
        """@brief 查询 metric 窗口统计 / Query metric window statistics."""

        rows = await self._fetch(_METRICS_SQL, window.start, window.end, name, limit)
        return tuple(_metric_stats(row) for row in rows)

    async def gen_ai(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[GenAiStats]:
        """@brief 查询 GenAI provider/model 统计 / Query GenAI provider/model statistics."""

        rows = await self._fetch(_GEN_AI_SQL, window.start, window.end, limit)
        return tuple(
            GenAiStats(
                provider=str(row["provider"]),
                model=str(row["model"]),
                calls=int(row["calls"]),
                errors=int(row["errors"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                p50_ms=float(row["p50_ms"]),
                p95_ms=float(row["p95_ms"]),
            )
            for row in rows
        )

    async def retrieval(self, window: TimeWindow) -> RetrievalSnapshot:
        """@brief 查询 Retrieval RED、队列与指标 / Query Retrieval RED, queues, and metrics."""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            operation_rows = await connection.fetch(
                _RETRIEVAL_OPERATIONS_SQL,
                window.start,
                window.end,
            )
            queue_rows = await connection.fetch(_RETRIEVAL_QUEUE_SQL)
            metric_rows = await connection.fetch(
                _RETRIEVAL_METRICS_SQL,
                window.start,
                window.end,
            )
        return RetrievalSnapshot(
            operations=tuple(
                SpanStats(
                    name=str(row["span_name"]),
                    kind=str(row["span_kind"]),
                    calls=int(row["calls"]),
                    rate_per_second=float(row["rate_per_second"]),
                    errors=int(row["errors"]),
                    p50_ms=float(row["p50_ms"]),
                    p95_ms=float(row["p95_ms"]),
                    p99_ms=float(row["p99_ms"]),
                    average_ms=float(row["average_ms"]),
                    maximum_ms=float(row["maximum_ms"]),
                )
                for row in operation_rows
            ),
            queues=tuple(
                RetrievalQueueStats(
                    space_id=str(row["space_id"]),
                    model=str(row["model"]),
                    dimensions=int(row["dimensions"]),
                    pending=int(row["pending_count"]),
                    processing=int(row["processing_count"]),
                    retrying=int(row["retry_count"]),
                    completed=int(row["completed_count"]),
                    failed_final=int(row["failed_final_count"]),
                    oldest_ready_at=(
                        _datetime(row["oldest_ready_at"])
                        if row["oldest_ready_at"] is not None
                        else None
                    ),
                    oldest_ready_age_seconds=_optional_float(
                        row["oldest_ready_age_seconds"]
                    ),
                    expired_leases=int(row["expired_lease_count"]),
                )
                for row in queue_rows
            ),
            metrics=tuple(_metric_stats(row) for row in metric_rows),
        )

    async def latency(self, window: TimeWindow) -> Sequence[TurnLatencyStats]:
        """@brief 查询 Turn 状态延迟统计 / Query Turn-state latency statistics."""

        rows = await self._fetch(_LATENCY_SQL, window.start, window.end)
        return tuple(
            TurnLatencyStats(
                state=str(row["state"]),
                turns=int(row["turns"]),
                p50_end_to_end_ms=_optional_float(row["p50_end_to_end_ms"]),
                p95_end_to_end_ms=_optional_float(row["p95_end_to_end_ms"]),
                p95_inference_ms=_optional_float(row["p95_inference_ms"]),
                p95_delivery_ms=_optional_float(row["p95_delivery_ms"]),
                average_inference_attempts=float(row["average_inference_attempts"]),
                average_delivery_attempts=float(row["average_delivery_attempts"]),
            )
            for row in rows
        )

    async def slow_turns(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[SlowTurn]:
        """@brief 查询慢 Turn / Query slow Turns."""

        rows = await self._fetch(_SLOW_TURNS_SQL, window.start, window.end, limit)
        return tuple(
            SlowTurn(
                turn_id=_uuid(row["turn_id"]),
                update_id=int(row["update_id"])
                if row["update_id"] is not None
                else None,
                state=str(row["state"]),
                received_at=(
                    _datetime(row["received_at"])
                    if row["received_at"] is not None
                    else None
                ),
                end_to_end_ms=_optional_float(row["end_to_end_ms"]),
                inference_ms=_optional_float(row["inference_total_ms"]),
                delivery_ms=_optional_float(row["delivery_total_ms"]),
                inference_attempts=int(row["inference_attempts"]),
                delivery_attempts=int(row["delivery_attempts"]),
            )
            for row in rows
        )

    async def resources(
        self,
        window: TimeWindow,
        *,
        limit: int,
    ) -> Sequence[ResourceInstance]:
        """@brief 查询资源生命周期 / Query resource lifecycles."""

        rows = await self._fetch(_RESOURCES_SQL, window.start, window.end, limit)
        return tuple(
            ResourceInstance(
                resource_id=_uuid(row["resource_id"]),
                service_name=str(row["service_name"]),
                service_version=str(row["service_version"]),
                environment=str(row["deployment_environment"]),
                instance_id=str(row["service_instance_id"]),
                started_at=_datetime(row["started_at"]),
                stopped_at=(
                    _datetime(row["stopped_at"])
                    if row["stopped_at"] is not None
                    else None
                ),
                attributes=freeze_json_object(row["attributes"]),
            )
            for row in rows
        )

    async def close(self) -> None:
        """@brief 关闭惰性连接池 / Close the lazy connection pool."""

        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    async def _fetch(self, query: str, *args: object) -> Sequence[Mapping[str, Any]]:
        """@brief 执行有界只读查询 / Execute a bounded read-only query."""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(query, *args)
        return cast(Sequence[Mapping[str, Any]], rows)

    async def _get_pool(self) -> asyncpg.Pool:
        """@brief 惰性创建只读连接池 / Lazily create the read-only pool."""

        if self._pool is None:
            timeout_ms = max(1, int(self._command_timeout * 1000))
            pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=0,
                max_size=self._pool_size,
                command_timeout=self._command_timeout,
                server_settings={
                    "application_name": "fogmoe-dashboard",
                    "default_transaction_read_only": "on",
                    "idle_in_transaction_session_timeout": "5000",
                    "lock_timeout": "1000",
                    "statement_timeout": str(timeout_ms),
                    "timezone": "UTC",
                },
                init=_init_connection,
            )
            if pool is None:
                raise RuntimeError("asyncpg did not create a dashboard pool")
            self._pool = pool
        return self._pool


async def _init_connection(connection: asyncpg.Connection) -> None:
    """@brief 为 JSONB 安装类型安全 codec / Install a type-safe JSONB codec."""

    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=lambda value: json.dumps(value, ensure_ascii=False),
        decoder=json.loads,
        format="text",
    )


def _pipeline_stage(row: Mapping[str, Any]) -> PipelineStage:
    """@brief 映射 pipeline row / Map a pipeline row."""

    return PipelineStage(
        stage=str(row["stage"]),
        pending=int(row["pending_count"]),
        processing=int(row["processing_count"]),
        retrying=int(row["retry_count"]),
        failed_final=int(row["failed_final_count"]),
        oldest_ready_at=(
            _datetime(row["oldest_ready_at"])
            if row["oldest_ready_at"] is not None
            else None
        ),
        expired_leases=int(row["expired_lease_count"]),
    )


def _metric_stats(row: Mapping[str, Any]) -> MetricStats:
    """@brief 映射 metric 聚合行 / Map a metric-aggregate row.

    @param row PostgreSQL 聚合结果 / PostgreSQL aggregate row.
    @return 强类型 metric 摘要 / Strongly typed metric summary.
    """

    return MetricStats(
        name=str(row["metric_name"]),
        kind=str(row["metric_kind"]),
        unit=str(row["unit"]),
        attributes=freeze_json_object(row["attributes"]),
        points=int(row["points"]),
        latest_at=_datetime(row["latest_at"]),
        latest=float(row["latest"]),
        minimum=float(row["minimum"]),
        maximum=float(row["maximum"]),
        average=float(row["average"]),
        total=_optional_float(row["total"]),
        rate_per_second=_optional_float(row["rate_per_second"]),
    )


def _datetime(value: object) -> datetime:
    """@brief 校验 UTC datetime / Validate a UTC datetime."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("Expected a timezone-aware datetime")
    return value.astimezone(UTC)


def _uuid(value: object) -> UUID:
    """@brief 校验 UUID / Validate a UUID."""

    if not isinstance(value, UUID):
        raise TypeError("Expected a UUID")
    return value


def _optional_uuid(value: object) -> UUID | None:
    """@brief 校验可选 UUID / Validate an optional UUID."""

    return None if value is None else _uuid(value)


def _optional_string(value: object) -> str | None:
    """@brief 映射可选字符串 / Map an optional string."""

    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    """@brief 映射可选浮点数 / Map an optional float."""

    return None if value is None else float(value)  # type: ignore[arg-type]


_OVERVIEW_SQL = """
WITH span_rollup AS (
  SELECT count(*) AS spans,
         count(*) FILTER (WHERE status_code = 'error') AS error_spans,
         count(DISTINCT trace_id) AS traces,
         percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p50_ms,
         percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms,
         percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p99_ms,
         coalesce(sum(CASE
           WHEN jsonb_typeof(attributes -> 'gen_ai.usage.input_tokens') = 'number'
           THEN (attributes ->> 'gen_ai.usage.input_tokens')::BIGINT ELSE 0 END), 0)
           AS input_tokens,
         coalesce(sum(CASE
           WHEN jsonb_typeof(attributes -> 'gen_ai.usage.output_tokens') = 'number'
           THEN (attributes ->> 'gen_ai.usage.output_tokens')::BIGINT ELSE 0 END), 0)
           AS output_tokens,
         count(*) FILTER (WHERE span_name = 'agent.tool.execute') AS tool_calls
  FROM observability.spans
  WHERE started_at >= $1 AND started_at < $2
    AND coalesce(attributes ->> 'db.system.name', '') <> 'postgresql'
), log_rollup AS (
  SELECT count(*) AS logs,
         count(*) FILTER (WHERE severity_number >= 17) AS error_logs
  FROM observability.log_records
  WHERE occurred_at >= $1 AND occurred_at < $2
)
SELECT CURRENT_TIMESTAMP AS generated_at,
       span_rollup.*, log_rollup.*
FROM span_rollup CROSS JOIN log_rollup
"""
"""@brief RED 总览聚合 SQL / RED overview aggregation SQL."""

_PIPELINE_SQL = """
SELECT * FROM (
  SELECT stage, pending_count, processing_count, retry_count,
         failed_final_count, oldest_ready_at, expired_lease_count
  FROM observability.pipeline_health
  UNION ALL
  SELECT 'retrieval.embedding' AS stage,
         count(*) FILTER (WHERE status = 'pending') AS pending_count,
         count(*) FILTER (WHERE status = 'processing') AS processing_count,
         count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
         count(*) FILTER (WHERE status = 'failed_final') AS failed_final_count,
         min(next_attempt_at) FILTER (WHERE status IN ('pending','retry_wait'))
           AS oldest_ready_at,
         count(*) FILTER (
           WHERE status = 'processing' AND lease_expires_at <= CURRENT_TIMESTAMP
         ) AS expired_lease_count
  FROM retrieval.passage_vectors
) AS pipeline
ORDER BY CASE stage
  WHEN 'inbox' THEN 1 WHEN 'inference' THEN 2 WHEN 'outbox' THEN 3 ELSE 4 END
"""
"""@brief Durable pipeline 健康 SQL / Durable-pipeline health SQL."""

_HEALTH_SERIES_SQL = """
WITH span_rollup AS (
  SELECT floor(
           EXTRACT(EPOCH FROM (started_at - $1::TIMESTAMPTZ)) / $3::INTEGER
         )::BIGINT AS bucket,
         count(*) AS spans,
         count(*) FILTER (WHERE status_code = 'error') AS error_spans,
         percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms,
         coalesce(sum(CASE
           WHEN jsonb_typeof(attributes -> 'gen_ai.usage.input_tokens') = 'number'
           THEN (attributes ->> 'gen_ai.usage.input_tokens')::BIGINT ELSE 0 END), 0)
           AS input_tokens,
         coalesce(sum(CASE
           WHEN jsonb_typeof(attributes -> 'gen_ai.usage.output_tokens') = 'number'
           THEN (attributes ->> 'gen_ai.usage.output_tokens')::BIGINT ELSE 0 END), 0)
           AS output_tokens
  FROM observability.spans
  WHERE started_at >= $1 AND started_at < $2
    AND coalesce(attributes ->> 'db.system.name', '') <> 'postgresql'
  GROUP BY bucket
), log_rollup AS (
  SELECT floor(
           EXTRACT(EPOCH FROM (occurred_at - $1::TIMESTAMPTZ)) / $3::INTEGER
         )::BIGINT AS bucket,
         count(*) FILTER (WHERE severity_number >= 17) AS error_logs
  FROM observability.log_records
  WHERE occurred_at >= $1 AND occurred_at < $2
  GROUP BY bucket
)
SELECT $1::TIMESTAMPTZ
         + make_interval(secs => coalesce(span_rollup.bucket, log_rollup.bucket)::INTEGER
                                  * $3::INTEGER) AS observed_at,
       coalesce(span_rollup.spans, 0)::DOUBLE PRECISION / $3::INTEGER
         AS span_rate_per_second,
       CASE WHEN coalesce(span_rollup.spans, 0) = 0 THEN 0
            ELSE span_rollup.error_spans::DOUBLE PRECISION / span_rollup.spans END
         AS span_error_rate,
       span_rollup.p95_ms,
       coalesce(log_rollup.error_logs, 0) AS error_logs,
       coalesce(span_rollup.input_tokens, 0) AS input_tokens,
       coalesce(span_rollup.output_tokens, 0) AS output_tokens
FROM span_rollup
FULL OUTER JOIN log_rollup USING (bucket)
ORDER BY observed_at
"""
"""@brief 健康时间序列 SQL / Health time-series SQL."""

_SPANS_SQL = """
SELECT span_name, span_kind, count(*) AS calls,
       count(*) / greatest(
         EXTRACT(EPOCH FROM ($2::TIMESTAMPTZ - $1::TIMESTAMPTZ)), 0.001
       ) AS rate_per_second,
       count(*) FILTER (WHERE status_code = 'error') AS errors,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p99_ms,
       avg(duration_ns / 1e6) AS average_ms,
       max(duration_ns / 1e6) AS maximum_ms
FROM observability.spans
WHERE started_at >= $1 AND started_at < $2
  AND ($3::TEXT IS NULL OR span_name = $3)
GROUP BY span_name, span_kind
ORDER BY p95_ms DESC, calls DESC
LIMIT $4
"""
"""@brief Span RED 分组 SQL / Span RED grouping SQL."""

_RETRIEVAL_OPERATIONS_SQL = """
SELECT span_name, span_kind, count(*) AS calls,
       count(*) / greatest(
         EXTRACT(EPOCH FROM ($2::TIMESTAMPTZ - $1::TIMESTAMPTZ)), 0.001
       ) AS rate_per_second,
       count(*) FILTER (WHERE status_code = 'error') AS errors,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p99_ms,
       avg(duration_ns / 1e6) AS average_ms,
       max(duration_ns / 1e6) AS maximum_ms
FROM observability.spans
WHERE started_at >= $1 AND started_at < $2
  AND span_name IN (
    'retrieval.projection.batch',
    'retrieval.embedding.batch',
    'retrieval.embedding.request',
    'retrieval.recall',
    'retrieval.query.embedding',
    'retrieval.search'
  )
GROUP BY span_name, span_kind
ORDER BY p95_ms DESC, calls DESC
"""
"""@brief Retrieval operation RED 聚合 / Retrieval-operation RED aggregation."""

_RETRIEVAL_QUEUE_SQL = """
SELECT space.space_id, space.model, space.dimensions,
       count(vector.passage_id) FILTER (WHERE vector.status = 'pending') AS pending_count,
       count(vector.passage_id) FILTER (WHERE vector.status = 'processing') AS processing_count,
       count(vector.passage_id) FILTER (WHERE vector.status = 'retry_wait') AS retry_count,
       count(vector.passage_id) FILTER (WHERE vector.status = 'completed') AS completed_count,
       count(vector.passage_id) FILTER (WHERE vector.status = 'failed_final')
         AS failed_final_count,
       min(vector.next_attempt_at) FILTER (
         WHERE vector.status IN ('pending','retry_wait')
       ) AS oldest_ready_at,
       EXTRACT(EPOCH FROM (
         CURRENT_TIMESTAMP - min(vector.next_attempt_at) FILTER (
           WHERE vector.status IN ('pending','retry_wait')
         )
       )) AS oldest_ready_age_seconds,
       count(vector.passage_id) FILTER (
         WHERE vector.status = 'processing'
           AND vector.lease_expires_at <= CURRENT_TIMESTAMP
       ) AS expired_lease_count
FROM retrieval.embedding_spaces AS space
LEFT JOIN retrieval.passage_vectors AS vector USING (space_id)
GROUP BY space.space_id, space.model, space.dimensions
ORDER BY space.space_id
"""
"""@brief Retrieval 当前队列健康 / Current Retrieval queue health."""

_ERRORS_SQL = """
SELECT occurred_at, source, name, message, trace_id, turn_id
FROM (
  SELECT started_at AS occurred_at, 'span'::TEXT AS source,
         span_name AS name,
         coalesce(nullif(status_message, ''), attributes ->> 'error.type', 'error') AS message,
         encode(trace_id, 'hex') AS trace_id, turn_id
  FROM observability.spans
  WHERE started_at >= $1 AND started_at < $2 AND status_code = 'error'
  UNION ALL
  SELECT occurred_at, 'log', coalesce(event_name, logger_name),
         coalesce(nullif(exception_message, ''), body),
         CASE WHEN trace_id IS NULL THEN NULL ELSE encode(trace_id, 'hex') END,
         turn_id
  FROM observability.log_records
  WHERE occurred_at >= $1 AND occurred_at < $2 AND severity_number >= 17
) AS errors
ORDER BY occurred_at DESC
LIMIT $3
"""
"""@brief 合并错误流 SQL / Merged error-stream SQL."""

_LOGS_SQL = """
SELECT occurred_at, severity_number, severity_text, logger_name, event_name, body,
       CASE WHEN trace_id IS NULL THEN NULL ELSE encode(trace_id, 'hex') END AS trace_id,
       CASE WHEN span_id IS NULL THEN NULL ELSE encode(span_id, 'hex') END AS span_id,
       turn_id
FROM observability.log_records
WHERE occurred_at >= $1 AND occurred_at < $2
  AND severity_number >= $3
  AND ($4::TEXT IS NULL OR logger_name = $4)
ORDER BY occurred_at DESC
LIMIT $5
"""
"""@brief 结构日志过滤 SQL / Structured-log filtering SQL."""

_TRACES_SQL = """
SELECT encode(trace_id, 'hex') AS trace_id,
       min(started_at) AS started_at, max(ended_at) AS ended_at,
       EXTRACT(EPOCH FROM (max(ended_at) - min(started_at))) * 1000 AS duration_ms,
       count(*) AS span_count,
       count(*) FILTER (WHERE status_code = 'error') AS error_count,
       coalesce(array_agg(DISTINCT span_name)
         FILTER (WHERE parent_span_id IS NULL), '{}'::TEXT[]) AS root_operations
FROM observability.spans
WHERE started_at >= $1 AND started_at < $2
  AND coalesce(attributes ->> 'db.system.name', '') <> 'postgresql'
GROUP BY trace_id
HAVING NOT $3::BOOLEAN OR count(*) FILTER (WHERE status_code = 'error') > 0
ORDER BY started_at DESC
LIMIT $4
"""
"""@brief Trace 摘要 SQL / Trace-summary SQL."""

_TRACE_SPANS_SQL = """
SELECT encode(span_id, 'hex') AS span_id,
       CASE WHEN parent_span_id IS NULL THEN NULL ELSE encode(parent_span_id, 'hex') END
         AS parent_span_id,
       span_name, span_kind, status_code, started_at, ended_at,
       duration_ns / 1e6 AS duration_ms, status_message, attributes
FROM observability.spans
WHERE trace_id = decode($1, 'hex')
ORDER BY started_at, span_id
"""
"""@brief Trace span waterfall SQL / Trace-span waterfall SQL."""

_TRACE_LOGS_SQL = """
SELECT occurred_at,
       CASE WHEN span_id IS NULL THEN NULL ELSE encode(span_id, 'hex') END AS span_id,
       severity_text, logger_name, event_name, body
FROM observability.log_records
WHERE trace_id = decode($1, 'hex')
ORDER BY occurred_at
"""
"""@brief Trace 关联日志 SQL / Trace-correlated log SQL."""

_METRICS_SQL = """
SELECT metric_name, metric_kind, unit, attributes, count(*) AS points,
       max(observed_at) AS latest_at,
       (array_agg(value ORDER BY observed_at DESC))[1] AS latest,
       min(value) AS minimum, max(value) AS maximum, avg(value) AS average,
       CASE WHEN metric_kind = 'counter' THEN sum(value) END AS total,
       CASE WHEN metric_kind = 'counter'
         THEN sum(value) / greatest(
           EXTRACT(EPOCH FROM ($2::TIMESTAMPTZ - $1::TIMESTAMPTZ)), 0.001
         )
       END AS rate_per_second
FROM observability.metric_points
WHERE observed_at >= $1 AND observed_at < $2
  AND ($3::TEXT IS NULL OR metric_name = $3)
GROUP BY metric_name, metric_kind, unit, attributes
ORDER BY metric_name, attributes
LIMIT $4
"""
"""@brief Metric 窗口统计 SQL / Metric-window statistics SQL."""

_RETRIEVAL_METRICS_SQL = """
SELECT metric_name, metric_kind, unit, attributes, count(*) AS points,
       max(observed_at) AS latest_at,
       (array_agg(value ORDER BY observed_at DESC))[1] AS latest,
       min(value) AS minimum, max(value) AS maximum, avg(value) AS average,
       CASE WHEN metric_kind = 'counter' THEN sum(value) END AS total,
       CASE WHEN metric_kind = 'counter'
         THEN sum(value) / greatest(
           EXTRACT(EPOCH FROM ($2::TIMESTAMPTZ - $1::TIMESTAMPTZ)), 0.001
         )
       END AS rate_per_second
FROM observability.metric_points
WHERE observed_at >= $1 AND observed_at < $2
  AND metric_name IN (
    'fogmoe.retrieval.outcomes',
    'fogmoe.retrieval.batch.size',
    'fogmoe.retrieval.source.discovery.duration',
    'fogmoe.retrieval.vector.claim.duration'
  )
GROUP BY metric_name, metric_kind, unit, attributes
ORDER BY metric_name, attributes
"""
"""@brief Retrieval metric 窗口统计 / Retrieval metric-window statistics."""

_GEN_AI_SQL = """
SELECT coalesce(attributes ->> 'gen_ai.provider.name', 'unknown') AS provider,
       coalesce(attributes ->> 'gen_ai.request.model', 'unknown') AS model,
       count(*) AS calls,
       count(*) FILTER (WHERE status_code = 'error') AS errors,
       coalesce(sum(CASE
         WHEN jsonb_typeof(attributes -> 'gen_ai.usage.input_tokens') = 'number'
         THEN (attributes ->> 'gen_ai.usage.input_tokens')::BIGINT ELSE 0 END), 0)
         AS input_tokens,
       coalesce(sum(CASE
         WHEN jsonb_typeof(attributes -> 'gen_ai.usage.output_tokens') = 'number'
         THEN (attributes ->> 'gen_ai.usage.output_tokens')::BIGINT ELSE 0 END), 0)
         AS output_tokens,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms
FROM observability.spans
WHERE started_at >= $1 AND started_at < $2 AND span_name = 'chat'
GROUP BY provider, model
ORDER BY calls DESC
LIMIT $3
"""
"""@brief GenAI provider/model 聚合 SQL / GenAI provider/model aggregation SQL."""

_LATENCY_SQL = """
SELECT state, count(*) AS turns,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY end_to_end_ms)
         FILTER (WHERE end_to_end_ms IS NOT NULL) AS p50_end_to_end_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY end_to_end_ms)
         FILTER (WHERE end_to_end_ms IS NOT NULL) AS p95_end_to_end_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY inference_total_ms)
         FILTER (WHERE inference_total_ms IS NOT NULL) AS p95_inference_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY delivery_total_ms)
         FILTER (WHERE delivery_total_ms IS NOT NULL) AS p95_delivery_ms,
       avg(inference_attempts) AS average_inference_attempts,
       avg(delivery_attempts) AS average_delivery_attempts
FROM observability.turn_latency
WHERE coalesce(received_at, turn_created_at) >= $1
  AND coalesce(received_at, turn_created_at) < $2
GROUP BY state
ORDER BY turns DESC
"""
"""@brief Turn 延迟分布 SQL / Turn-latency distribution SQL."""

_SLOW_TURNS_SQL = """
SELECT turn_id, update_id, state, received_at, end_to_end_ms,
       inference_total_ms, delivery_total_ms,
       inference_attempts, delivery_attempts
FROM observability.turn_latency
WHERE coalesce(received_at, turn_created_at) >= $1
  AND coalesce(received_at, turn_created_at) < $2
ORDER BY end_to_end_ms DESC NULLS LAST, turn_created_at DESC
LIMIT $3
"""
"""@brief 慢 Turn SQL / Slow-Turn SQL."""

_RESOURCES_SQL = """
SELECT resource_id, service_name, service_version, deployment_environment,
       service_instance_id, started_at, stopped_at, attributes
FROM observability.resources
WHERE started_at < $2 AND coalesce(stopped_at, $2) >= $1
ORDER BY started_at DESC
LIMIT $3
"""
"""@brief 资源实例生命周期 SQL / Resource-instance lifecycle SQL."""


__all__ = ["PostgresDashboardRepository"]
