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
from fogmoe_bot.infrastructure.observability.postgres import PostgresTelemetrySink
from postgres_test_support import database_settings_from_url


def _asyncpg_database_url() -> str:
    """@brief 读取显式测试 DSN 并转换为 asyncpg URL / Read an explicit test DSN and convert it to an asyncpg URL.

    @return asyncpg 数据库 URL / asyncpg database URL.
    """

    raw_url = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if raw_url:
        return database_settings_from_url(raw_url).asyncpg_url()
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def test_postgres_sink_atomically_persists_every_signal_kind() -> None:
    """@brief 独立 exporter 写入日志、span、metric 与资源终态 / The isolated exporter writes logs, spans, metrics, and resource termination."""

    async def scenario() -> None:
        """@brief 执行真实 PostgreSQL 往返 / Execute a real PostgreSQL round trip."""

        database_url = _asyncpg_database_url()
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
            dsn=database_url,
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

        connection = await asyncpg.connect(database_url)
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
        connection = await asyncpg.connect(database_url)
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
