"""@brief Dashboard PostgreSQL 端到端测试 / Dashboard PostgreSQL end-to-end tests."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
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
from fogmoe_dashboard.api import DashboardClient
from fogmoe_dashboard.domain.models import TimeWindow
from fogmoe_dashboard.infrastructure.postgres import PostgresDashboardRepository


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_TESTS") != "1",
    reason="requires an explicitly migrated PostgreSQL test database",
)


def test_dashboard_queries_every_signal_family_through_read_only_pool() -> None:
    """@brief Dashboard 跨 logs/spans/metrics 提供 drill-down / Dashboard provides drill-down across logs, spans, and metrics."""

    async def scenario() -> None:
        """@brief 执行完整数据库往返 / Execute the complete database round trip."""

        now = datetime.now(UTC)
        trace = TraceContext.new_root()
        resource = Resource(
            resource_id=uuid4(),
            service_name="fogmoe-dashboard-test",
            service_version="test",
            deployment_environment="test",
            service_instance_id=str(uuid4()),
            started_at=now,
            attributes=freeze_attributes({"test.kind": "dashboard"}),
        )
        sink = PostgresTelemetrySink(
            dsn=config.asyncpg_database_uri(),
            resource=resource,
            command_timeout=5,
            retention_days=30,
        )
        attributes = freeze_attributes(
            {
                "gen_ai.provider.name": "test-provider",
                "gen_ai.request.model": "test-model",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 30,
                "error.type": "TestFailure",
            }
        )
        await sink.write(
            (
                SpanSignal(
                    started_at=now,
                    ended_at=now + timedelta(milliseconds=25),
                    duration_ns=25_000_000,
                    trace_id=trace.trace_id,
                    span_id=trace.span_id,
                    parent_span_id=None,
                    name="chat",
                    kind=SpanKind.CLIENT,
                    status=SpanStatus.ERROR,
                    status_message="provider failed",
                    attributes=attributes,
                ),
                LogSignal(
                    occurred_at=now,
                    observed_at=now,
                    severity=Severity.ERROR,
                    severity_text="ERROR",
                    logger_name="fogmoe.dashboard.test",
                    body="request failed",
                    event_name="test.failed",
                    trace_id=trace.trace_id,
                    span_id=trace.span_id,
                    exception_type="TestFailure",
                    exception_message="provider failed",
                    exception_stack=None,
                    attributes=freeze_attributes(),
                ),
                MetricSignal(
                    observed_at=now,
                    name="fogmoe.dashboard.test",
                    kind=MetricKind.GAUGE,
                    value=7,
                    unit="{item}",
                    trace_id=trace.trace_id,
                    attributes=freeze_attributes(),
                ),
            )
        )

        window = TimeWindow(now - timedelta(minutes=1), now + timedelta(minutes=1))
        async with DashboardClient.from_database_url(
            config.asyncpg_database_uri()
        ) as dashboard:
            overview = await dashboard.overview(window)
            assert overview.spans >= 1
            assert overview.error_spans >= 1
            assert overview.input_tokens >= 120
            assert len(overview.pipeline) == 3

            spans = await dashboard.spans(window, name="chat")
            assert any(item.errors >= 1 for item in spans)
            assert len(await dashboard.errors(window)) >= 2
            assert any(
                item.event_name == "test.failed"
                for item in await dashboard.logs(window, minimum_severity=17)
            )
            summaries = await dashboard.traces(window, errors_only=True)
            trace_id = str(trace.trace_id)
            assert any(item.trace_id == trace_id for item in summaries)
            detail = await dashboard.trace(trace_id)
            assert [item.name for item in detail.spans] == ["chat"]
            assert [item.event_name for item in detail.logs] == ["test.failed"]
            assert any(
                item.name == "fogmoe.dashboard.test"
                for item in await dashboard.metrics(window)
            )
            assert any(
                item.provider == "test-provider" and item.input_tokens >= 120
                for item in await dashboard.gen_ai(window)
            )
            assert any(
                item.resource_id == resource.resource_id
                for item in await dashboard.resources(window)
            )
            await dashboard.latency(window)
            await dashboard.slow_turns(window)

        repository = PostgresDashboardRepository(config.asyncpg_database_uri())
        read_only = await repository._fetch("SHOW default_transaction_read_only")
        assert read_only[0]["default_transaction_read_only"] == "on"
        await repository.close()
        await sink.close()

        connection = await asyncpg.connect(config.asyncpg_database_uri())
        try:
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
