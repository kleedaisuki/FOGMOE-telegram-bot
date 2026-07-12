"""@brief PostgreSQL observability exporter 集成测试 / PostgreSQL observability-exporter integration tests."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from fogmoe_bot.domain.observability.signals import (
    LogSignal,
    MetricKind,
    MetricSignal,
    Resource,
    Severity,
    SpanKind,
    SpanSignal,
    SpanStatus,
    freeze_attributes,
)
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.observability.postgres import PostgresTelemetrySink


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_TESTS") != "1",
    reason="requires an explicitly migrated PostgreSQL test database",
)
"""@brief 防止测试误写非测试数据库 / Prevent accidental writes to a non-test database."""


def test_postgres_sink_atomically_persists_every_signal_kind() -> None:
    """@brief 独立 exporter 写入日志、span、metric 与资源终态 / The isolated exporter writes logs, spans, metrics, and resource termination."""

    async def scenario() -> None:
        """@brief 执行真实 PostgreSQL 往返 / Execute a real PostgreSQL round trip."""

        now = datetime.now(UTC)
        context = TraceContext.new_root()
        resource = Resource(
            resource_id=uuid4(),
            service_name="fogmoe-observability-test",
            service_version="test",
            deployment_environment="test",
            service_instance_id=str(uuid4()),
            started_at=now,
            attributes=freeze_attributes({"test.kind": "integration"}),
        )
        sink = PostgresTelemetrySink(
            dsn=config.asyncpg_database_uri(),
            resource=resource,
            command_timeout=5,
            retention_days=30,
        )
        attributes = freeze_attributes({"fogmoe.update.id": 42})
        await sink.write(
            (
                LogSignal(
                    occurred_at=now,
                    observed_at=now,
                    severity=Severity.INFO,
                    severity_text="INFO",
                    logger_name="fogmoe.integration",
                    body="hello",
                    event_name="integration.started",
                    trace_id=context.trace_id,
                    span_id=context.span_id,
                    exception_type=None,
                    exception_message=None,
                    exception_stack=None,
                    attributes=attributes,
                ),
                SpanSignal(
                    started_at=now,
                    ended_at=now,
                    duration_ns=0,
                    trace_id=context.trace_id,
                    span_id=context.span_id,
                    parent_span_id=None,
                    name="integration",
                    kind=SpanKind.INTERNAL,
                    status=SpanStatus.OK,
                    status_message=None,
                    attributes=attributes,
                ),
                MetricSignal(
                    observed_at=now,
                    name="fogmoe.integration",
                    kind=MetricKind.GAUGE,
                    value=1,
                    unit="1",
                    trace_id=context.trace_id,
                    attributes=attributes,
                ),
            )
        )
        await sink.write(
            (
                MetricSignal(
                    observed_at=now,
                    name="fogmoe.integration.second_batch",
                    kind=MetricKind.COUNTER,
                    value=1,
                    unit="{event}",
                    trace_id=context.trace_id,
                    attributes=attributes,
                ),
            )
        )

        connection = await asyncpg.connect(config.asyncpg_database_uri())
        try:
            counts = await connection.fetchrow(
                "SELECT "
                "(SELECT count(*) FROM observability.log_records WHERE resource_id = $1), "
                "(SELECT count(*) FROM observability.spans WHERE resource_id = $1), "
                "(SELECT count(*) FROM observability.metric_points WHERE resource_id = $1)",
                resource.resource_id,
            )
            assert counts is not None
            assert tuple(counts) == (1, 1, 2)
        finally:
            await connection.close()

        await sink.close()
        connection = await asyncpg.connect(config.asyncpg_database_uri())
        try:
            stopped_at = await connection.fetchval(
                "SELECT stopped_at FROM observability.resources WHERE resource_id = $1",
                resource.resource_id,
            )
            assert stopped_at is not None
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM observability.log_records WHERE resource_id = $1",
                    resource.resource_id,
                )
                await connection.execute(
                    "DELETE FROM observability.spans WHERE resource_id = $1",
                    resource.resource_id,
                )
                await connection.execute(
                    "DELETE FROM observability.metric_points WHERE resource_id = $1",
                    resource.resource_id,
                )
                await connection.execute(
                    "DELETE FROM observability.resources WHERE resource_id = $1",
                    resource.resource_id,
                )
        finally:
            await connection.close()

    asyncio.run(scenario())
