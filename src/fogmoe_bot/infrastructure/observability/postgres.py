"""@brief PostgreSQL 遥测批量存储 / PostgreSQL batched telemetry storage."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from fogmoe_bot.domain.observability.signals import (
    AttributeValue,
    LogSignal,
    MetricSignal,
    Resource,
    SpanSignal,
    TelemetrySignal,
)


class PostgresTelemetrySink:
    """@brief 用独立单连接池原子写入遥测批次 / Atomically write telemetry batches through an isolated single-connection pool."""

    def __init__(
        self,
        *,
        dsn: str,
        resource: Resource,
        command_timeout: float,
        retention_days: int,
    ) -> None:
        """@brief 保存惰性连接配置 / Store lazy connection configuration.

        @param dsn asyncpg DSN / Asyncpg DSN.
        @param resource 本进程资源 / Current process resource.
        @param command_timeout 单次数据库命令秒数 / Per-command database timeout in seconds.
        """

        if not dsn.strip() or command_timeout <= 0 or retention_days < 1:
            raise ValueError(
                "Telemetry PostgreSQL settings must be positive and nonblank"
            )
        self._dsn = dsn
        self._resource = resource
        self._command_timeout = command_timeout
        self._retention_days = retention_days
        self._pool: asyncpg.Pool | None = None
        self._maintenance_day: date | None = None
        self._ensured_days: set[date] = set()

    async def write(self, signals: Sequence[TelemetrySignal]) -> None:
        """@brief 在一个短事务中写入完整批次 / Write a complete batch in one short transaction.

        @param signals 非空或空信号序列 / Non-empty or empty signal sequence.
        @return None / None.
        """

        if not signals:
            return
        pool = await self._get_pool()
        today = datetime.now(UTC).date()
        maintenance_needed = self._maintenance_day != today
        signal_days = {_signal_time(signal).date() for signal in signals}
        new_days = signal_days - self._ensured_days
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._upsert_resource(cast(asyncpg.Connection, connection))
                if maintenance_needed:
                    await connection.execute(
                        "SELECT observability.drop_partitions_before($1::date)",
                        today - timedelta(days=self._retention_days),
                    )
                for day in sorted(new_days):
                    await connection.execute(
                        "SELECT observability.ensure_daily_partitions($1::date)",
                        day,
                    )
                logs = tuple(
                    signal for signal in signals if isinstance(signal, LogSignal)
                )
                spans = tuple(
                    signal for signal in signals if isinstance(signal, SpanSignal)
                )
                metrics = tuple(
                    signal for signal in signals if isinstance(signal, MetricSignal)
                )
                if logs:
                    await connection.executemany(_INSERT_LOG, map(self._log_row, logs))
                if spans:
                    await connection.executemany(
                        _INSERT_SPAN, map(self._span_row, spans)
                    )
                if metrics:
                    await connection.executemany(
                        _INSERT_METRIC,
                        map(self._metric_row, metrics),
                    )
            if maintenance_needed:
                self._maintenance_day = today
            self._ensured_days.update(new_days)

    async def close(self) -> None:
        """@brief 关闭独立连接池 / Close the isolated pool.

        @return None / None.
        """

        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                async with pool.acquire() as connection:
                    await connection.execute(
                        "UPDATE observability.resources SET stopped_at = $1 "
                        "WHERE resource_id = $2 AND stopped_at IS NULL",
                        datetime.now(UTC),
                        self._resource.resource_id,
                    )
            finally:
                await pool.close()

    async def _get_pool(self) -> asyncpg.Pool:
        """@brief 惰性建立最多一个连接的池 / Lazily create a pool with at most one connection."""

        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=0,
                max_size=1,
                command_timeout=self._command_timeout,
            )
        return self._pool

    async def _upsert_resource(self, connection: asyncpg.Connection) -> None:
        """@brief 幂等记录进程资源 / Idempotently record the process resource."""

        resource = self._resource
        await connection.execute(
            "INSERT INTO observability.resources "
            "(resource_id, service_name, service_version, deployment_environment, "
            "service_instance_id, started_at, attributes) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb) "
            "ON CONFLICT (resource_id) DO NOTHING",
            resource.resource_id,
            resource.service_name,
            resource.service_version,
            resource.deployment_environment,
            resource.service_instance_id,
            resource.started_at,
            _json(resource.attributes),
        )

    def _log_row(self, signal: LogSignal) -> tuple[object, ...]:
        """@brief 将日志映射为 SQL 参数 / Map a log to SQL parameters."""

        return (
            signal.occurred_at,
            signal.observed_at,
            self._resource.resource_id,
            signal.trace_id.value if signal.trace_id else None,
            signal.span_id.value if signal.span_id else None,
            int(signal.severity),
            signal.severity_text,
            signal.logger_name,
            signal.event_name,
            signal.body,
            signal.exception_type,
            signal.exception_message,
            signal.exception_stack,
            _uuid_attribute(signal.attributes, "fogmoe.turn.id"),
            _integer_attribute(signal.attributes, "fogmoe.update.id"),
            _uuid_attribute(signal.attributes, "fogmoe.activity.id"),
            _uuid_attribute(signal.attributes, "fogmoe.outbound.id"),
            _json(signal.attributes),
        )

    def _span_row(self, signal: SpanSignal) -> tuple[object, ...]:
        """@brief 将 span 映射为 SQL 参数 / Map a span to SQL parameters."""

        return (
            signal.started_at,
            signal.ended_at,
            signal.duration_ns,
            self._resource.resource_id,
            signal.trace_id.value,
            signal.span_id.value,
            signal.parent_span_id.value if signal.parent_span_id else None,
            signal.name,
            signal.kind.value,
            signal.status.value,
            signal.status_message,
            _uuid_attribute(signal.attributes, "fogmoe.turn.id"),
            _integer_attribute(signal.attributes, "fogmoe.update.id"),
            _uuid_attribute(signal.attributes, "fogmoe.activity.id"),
            _uuid_attribute(signal.attributes, "fogmoe.outbound.id"),
            _json(signal.attributes),
        )

    def _metric_row(self, signal: MetricSignal) -> tuple[object, ...]:
        """@brief 将 metric 映射为 SQL 参数 / Map a metric to SQL parameters."""

        return (
            signal.observed_at,
            self._resource.resource_id,
            signal.name,
            signal.kind.value,
            signal.value,
            signal.unit,
            signal.trace_id.value if signal.trace_id else None,
            _json(signal.attributes),
        )


class DiscardTelemetrySink:
    """@brief 显式禁用部署使用的排空 sink / Draining sink for explicitly disabled deployments."""

    async def write(self, signals: Sequence[TelemetrySignal]) -> None:
        """@brief 消费但不持久化 / Consume without persistence.

        @param signals 待丢弃批次 / Batch to discard.
        @return None / None.
        """

        del signals

    async def close(self) -> None:
        """@brief 无资源关闭 / Close no resources.

        @return None / None.
        """


def _signal_time(signal: TelemetrySignal) -> datetime:
    """@brief 返回信号分区时间 / Return a signal's partition time."""

    if isinstance(signal, LogSignal):
        return signal.occurred_at
    if isinstance(signal, SpanSignal):
        return signal.started_at
    return signal.observed_at


def _json(attributes: Mapping[str, AttributeValue]) -> str:
    """@brief 编码规范 JSONB / Encode canonical JSONB."""

    return json.dumps(
        dict(attributes),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _uuid_attribute(attributes: Mapping[str, AttributeValue], key: str) -> UUID | None:
    """@brief 读取可选 UUID 属性 / Read an optional UUID attribute."""

    value = attributes.get(key)
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _integer_attribute(
    attributes: Mapping[str, AttributeValue], key: str
) -> int | None:
    """@brief 读取可选整数属性 / Read an optional integer attribute."""

    value = attributes.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_INSERT_LOG = """
INSERT INTO observability.log_records (
  occurred_at, observed_at, resource_id, trace_id, span_id,
  severity_number, severity_text, logger_name, event_name, body,
  exception_type, exception_message, exception_stack,
  turn_id, update_id, activity_id, outbound_message_id, attributes
) VALUES (
  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
  $11, $12, $13, $14, $15, $16, $17, $18::jsonb
)
"""
"""@brief 结构化日志批量插入 / Structured-log batch insert."""

_INSERT_SPAN = """
INSERT INTO observability.spans (
  started_at, ended_at, duration_ns, resource_id, trace_id, span_id,
  parent_span_id, span_name, span_kind, status_code, status_message,
  turn_id, update_id, activity_id, outbound_message_id, attributes
) VALUES (
  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
  $12, $13, $14, $15, $16::jsonb
)
"""
"""@brief 操作 span 批量插入 / Operation-span batch insert."""

_INSERT_METRIC = """
INSERT INTO observability.metric_points (
  observed_at, resource_id, metric_name, metric_kind, value, unit,
  exemplar_trace_id, attributes
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
"""
"""@brief 原始 metric point 批量插入 / Raw metric-point batch insert."""


__all__ = ["DiscardTelemetrySink", "PostgresTelemetrySink"]
