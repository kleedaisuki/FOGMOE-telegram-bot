"""@brief Dashboard 领域、API 与 presentation 测试 / Dashboard domain, API, and presentation tests."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from rich.console import Console

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.application.queries import (
    DashboardView,
    RetrievalQuery,
    SpansQuery,
    execute_query,
)
from fogmoe_dashboard.api import DashboardClient
from fogmoe_dashboard.config import read_dashboard_settings
from fogmoe_dashboard.domain.models import (
    Overview,
    PipelineStage,
    ResourceInstance,
    ResourceState,
    RetrievalQueueStats,
    RetrievalSnapshot,
    SpanStats,
    TimeWindow,
)
from fogmoe_dashboard.infrastructure.postgres import PostgresDashboardRepository
from fogmoe_dashboard.presentation.cli import build_parser
from fogmoe_dashboard.presentation.duration import parse_duration
from fogmoe_dashboard.presentation.render import print_json, render, to_jsonable


class FakeRepository:
    """@brief 记录 Dashboard 查询的最小 fake / Minimal fake recording Dashboard queries."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.calls: list[tuple[str, object]] = []
        self.closed = False

    async def overview(self, window: TimeWindow) -> Overview:
        """@brief 返回固定总览 / Return a fixed overview."""

        self.calls.append(("overview", window))
        return _overview(window)

    async def pipeline(self):
        """@brief 返回固定 pipeline / Return a fixed pipeline."""

        return _overview(TimeWindow.last(timedelta(hours=1))).pipeline

    async def health_series(self, window, *, buckets):
        """@brief 返回空健康趋势 / Return an empty health trend."""

        self.calls.append(("health_series", (window, buckets)))
        return ()

    async def retrieval(self, window: TimeWindow) -> RetrievalSnapshot:
        """@brief 返回固定 Retrieval 快照 / Return a fixed Retrieval snapshot."""

        self.calls.append(("retrieval", window))
        return _retrieval()

    async def spans(self, window, *, name, limit):
        """@brief 记录 span 查询 / Record a span query."""

        self.calls.append(("spans", (window, name, limit)))
        return ()

    async def logs(self, window, *, minimum_severity, logger_name, limit):
        """@brief 记录日志查询 / Record a log query."""

        self.calls.append(("logs", (window, minimum_severity, logger_name, limit)))
        return ()

    async def trace(self, trace_id):
        """@brief 记录 trace 查询 / Record a trace query."""

        self.calls.append(("trace", trace_id))
        return trace_id

    async def close(self) -> None:
        """@brief 记录关闭 / Record closure."""

        self.closed = True


def test_time_window_normalizes_utc_and_rejects_unbounded_queries() -> None:
    """@brief 时间窗规范 UTC 且拒绝超大扫描 / Time windows normalize UTC and reject oversized scans."""

    now = datetime.now(UTC)
    window = TimeWindow(now - timedelta(hours=1), now)

    assert window.start.tzinfo is UTC
    with pytest.raises(ValueError):
        TimeWindow(now, now)
    with pytest.raises(ValueError):
        TimeWindow(now - timedelta(days=91), now)


def test_resource_liveness_uses_heartbeats_at_the_query_boundary() -> None:
    """@brief 资源存活性基于查询边界的心跳而非 stopped_at 空值 / Resource liveness uses the query-boundary heartbeat rather than null stopped_at."""

    observed_at = datetime(2026, 7, 22, 8, tzinfo=UTC)

    assert (
        ResourceState.at(
            last_seen_at=observed_at - timedelta(seconds=89),
            stopped_at=None,
            observed_at=observed_at,
        )
        is ResourceState.ACTIVE
    )
    assert (
        ResourceState.at(
            last_seen_at=observed_at - timedelta(seconds=91),
            stopped_at=None,
            observed_at=observed_at,
        )
        is ResourceState.STALE
    )
    assert (
        ResourceState.at(
            last_seen_at=observed_at,
            stopped_at=observed_at - timedelta(seconds=1),
            observed_at=observed_at,
        )
        is ResourceState.STOPPED
    )
    assert (
        ResourceState.at(
            last_seen_at=observed_at,
            stopped_at=observed_at + timedelta(seconds=1),
            observed_at=observed_at,
        )
        is ResourceState.ACTIVE
    )


def test_resource_view_renders_explicit_state_and_last_heartbeat() -> None:
    """@brief 资源视图明示状态与最后心跳 / The resource view exposes state and the latest heartbeat."""

    observed_at = datetime(2026, 7, 22, 8, tzinfo=UTC)
    resource = ResourceInstance(
        resource_id=uuid4(),
        service_name="fogmoe-bot",
        service_version="test",
        environment="test",
        instance_id="instance-1",
        started_at=observed_at - timedelta(minutes=5),
        last_seen_at=observed_at - timedelta(minutes=2),
        stopped_at=None,
        state=ResourceState.STALE,
        attributes={},
    )
    console = Console(record=True, width=160, color_system=None)

    console.print(render(DashboardView.RESOURCES, (resource,)))

    rendered = console.export_text()
    assert "State" in rendered
    assert "Last seen" in rendered
    assert "stale" in rendered


def test_dashboard_reads_its_jsonc_projection_and_builds_typed_client(
    tmp_path: Path,
) -> None:
    """@brief Dashboard 从根 JSONC 读取自身投影 / Dashboard reads its own projection from root JSONC.

    @param tmp_path pytest 临时目录 / pytest temporary directory.
    @return None / None.
    @note 配置边界只接受显式根配置文件路径。/
        The configuration boundary accepts only an explicit root-configuration path.
    """

    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          // Dashboard 只消费这些语义字段。
          "schema_version": 1,
          "database": {
            "endpoint": {
              "host": "analytics.internal",
              "port": 5544,
              "name": "fogmoe-analytics"
            },
            "reporting": {
              "username": "read+only",
              "password": "p:ass\\\\word"
            }
          },
          "observability": {
            "dashboard": {"pool_size": 7, "command_timeout_seconds": 9.5}
          }
        }
        """,
        encoding="utf-8",
    )

    settings = read_dashboard_settings(config_path)
    client = DashboardClient.from_database_settings(settings=settings)

    assert settings.endpoint.host == "analytics.internal"
    assert settings.query.pool_size == 7
    assert settings.database_url() == (
        "postgresql+asyncpg://read%2Bonly:p%3Aass%5Cword@"
        "analytics.internal:5544/fogmoe-analytics"
    )
    assert isinstance(client._repository, PostgresDashboardRepository)
    assert client._repository._dsn == (
        "postgresql://read%2Bonly:p%3Aass%5Cword@"
        "analytics.internal:5544/fogmoe-analytics"
    )
    assert client._repository._pool_size == 7
    assert client._repository._command_timeout == 9.5


def test_dashboard_enforces_limits_filters_and_trace_identity() -> None:
    """@brief 应用层在 repository 前约束查询 / The application layer bounds queries before the repository."""

    async def scenario() -> None:
        """@brief 执行类型化查询 / Execute typed queries."""

        repository = FakeRepository()
        dashboard = Dashboard(repository)  # type: ignore[arg-type]
        window = TimeWindow.last(timedelta(hours=1))

        await dashboard.spans(window, name=" chat ", limit=10)
        await dashboard.logs(
            window,
            minimum_severity=17,
            logger_name=" fogmoe.test ",
            limit=5,
        )
        assert await dashboard.trace("A" * 32) == "a" * 32
        with pytest.raises(ValueError):
            await dashboard.spans(window, limit=0)
        with pytest.raises(ValueError):
            await dashboard.logs(window, minimum_severity=25)
        with pytest.raises(ValueError):
            await dashboard.trace("not-a-trace")

        assert repository.calls[0][0] == "spans"
        assert repository.calls[0][1][1:] == ("chat", 10)
        await dashboard.close()
        assert repository.closed

    import asyncio

    asyncio.run(scenario())


def test_closed_query_language_reuses_application_semantics() -> None:
    """@brief CLI/GUI 查询语言复用同一应用约束 / CLI and GUI query language reuse the same application constraints."""

    async def scenario() -> None:
        """@brief 执行一个封闭 Span 查询 / Execute one closed Span query."""

        repository = FakeRepository()
        dashboard = Dashboard(repository)  # type: ignore[arg-type]
        window = TimeWindow.last(timedelta(minutes=15))

        result = await execute_query(
            dashboard,
            SpansQuery(window, name=" chat ", limit=12),
        )

        assert result == ()
        assert repository.calls[-1][1][1:] == ("chat", 12)
        retrieval = await execute_query(dashboard, RetrievalQuery(window))
        assert isinstance(retrieval, RetrievalSnapshot)
        assert retrieval.ready == 3

    import asyncio

    asyncio.run(scenario())


def test_dashboard_layer_dependencies_point_inward() -> None:
    """@brief Dashboard domain/application 不能依赖 adapter 或 GUI / Dashboard domain and application cannot depend on adapters or GUI."""

    package_root = Path(__file__).parents[1] / "src" / "fogmoe_dashboard"
    rules = {
        "domain": (
            "fogmoe_dashboard.application",
            "fogmoe_dashboard.infrastructure",
            "fogmoe_dashboard.presentation",
            "PyQt6",
            "matplotlib",
        ),
        "application": (
            "fogmoe_dashboard.infrastructure",
            "fogmoe_dashboard.presentation",
            "PyQt6",
            "matplotlib",
        ),
    }
    violations: list[str] = []
    for layer, forbidden in rules.items():
        for path in (package_root / layer).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: tuple[str, ...] = ()
                if isinstance(node, ast.ImportFrom):
                    modules = (node.module or "",)
                elif isinstance(node, ast.Import):
                    modules = tuple(alias.name for alias in node.names)
                if any(
                    module.startswith(prefix)
                    for module in modules
                    for prefix in forbidden
                ):
                    violations.append(f"{path.relative_to(package_root)}:{node.lineno}")

    assert violations == []


def test_dashboard_cli_registers_views_and_root_config_argument() -> None:
    """@brief CLI 暴露完整内建视图与根配置参数 / CLI exposes built-in views and root-config argument.

    @return None / None.
    """

    parser = build_parser()
    views = {
        "overview",
        "pipeline",
        "spans",
        "errors",
        "logs",
        "traces",
        "trace",
        "metrics",
        "ai",
        "retrieval",
        "latency",
        "resources",
        "watch",
    }

    assert {
        parser.parse_args([view, *(["0" * 32] if view == "trace" else [])]).view
        for view in views
    } == views
    arguments = parser.parse_args(["--config", "operator.json", "overview"])
    assert arguments.config == Path("operator.json")
    assert not {
        "database_url",
        "config_dir",
        "service",
        "timeout",
    }.intersection(vars(arguments))
    assert parse_duration("15m") == timedelta(minutes=15)
    assert parse_duration("1.5h") == timedelta(minutes=90)
    with pytest.raises(ValueError):
        parse_duration("yesterday")


def test_overview_renders_rich_table_and_stable_json() -> None:
    """@brief 同一模型可交互渲染也可脚本消费 / One model supports interactive rendering and script consumption."""

    value = _overview(TimeWindow.last(timedelta(hours=1)))
    table_console = Console(record=True, width=120, color_system=None)
    table_console.print(render(DashboardView.OVERVIEW, value))
    rendered = table_console.export_text()
    assert "FogMoe observability" in rendered
    assert "Durable pipeline" in rendered
    assert "inbox" in rendered

    json_console = Console(record=True, color_system=None)
    print_json(json_console, DashboardView.OVERVIEW, value)
    payload = json.loads(json_console.export_text())
    assert payload["schema_version"] == 1
    assert payload["view"] == "overview"
    assert payload["data"]["spans"] == 10
    assert to_jsonable(value.window)["start"].endswith("+00:00")

    retrieval_console = Console(record=True, width=160, color_system=None)
    retrieval_console.print(render(DashboardView.RETRIEVAL, _retrieval()))
    retrieval_text = retrieval_console.export_text()
    assert "Retrieval operations" in retrieval_text
    assert "Embedding queues" in retrieval_text
    assert "retrieval.recall" in retrieval_text
    assert "memory.working.retrieve" in retrieval_text


def _overview(window: TimeWindow) -> Overview:
    """@brief 创建固定总览 fixture / Create a fixed overview fixture."""

    return Overview(
        generated_at=window.end,
        window=window,
        spans=10,
        error_spans=1,
        traces=4,
        logs=20,
        error_logs=2,
        p50_ms=1.2,
        p95_ms=5.6,
        p99_ms=8.9,
        input_tokens=100,
        output_tokens=20,
        tool_calls=3,
        pipeline=(
            PipelineStage("inbox", 1, 2, 3, 4, None, 0),
            PipelineStage("inference", 0, 1, 0, 0, None, 0),
            PipelineStage("outbox", 2, 0, 0, 0, None, 0),
        ),
    )


def _retrieval() -> RetrievalSnapshot:
    """@brief 创建固定 Retrieval 快照 / Create a fixed Retrieval snapshot."""

    return RetrievalSnapshot(
        operations=(
            SpanStats("retrieval.recall", "internal", 4, 0.1, 0, 4, 8, 12, 6, 14),
            SpanStats(
                "memory.working.retrieve", "internal", 4, 0.1, 0, 5, 9, 13, 7, 15
            ),
        ),
        queues=(
            RetrievalQueueStats(
                "conversation.qwen.v1",
                "qwen/qwen3-embedding-8b",
                1024,
                2,
                1,
                1,
                100,
                0,
                None,
                12.0,
                0,
            ),
        ),
        metrics=(),
    )
