"""@brief FogMoe Dashboard 交互式 CLI / Interactive FogMoe Dashboard CLI."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from rich.console import Console
from rich.live import Live

from fogmoe_dashboard.api import DashboardClient
from fogmoe_dashboard.application.queries import (
    DashboardQuery,
    DashboardView,
    ErrorsQuery,
    GenAiQuery,
    LatencyQuery,
    LogsQuery,
    MetricsQuery,
    OverviewQuery,
    PipelineQuery,
    ResourcesQuery,
    RetrievalQuery,
    SpansQuery,
    TraceQuery,
    TracesQuery,
    execute_query,
)
from fogmoe_dashboard.config import default_config_path, read_dashboard_settings
from fogmoe_dashboard.domain.models import TimeWindow
from fogmoe_dashboard.presentation.duration import parse_duration
from fogmoe_dashboard.presentation.render import print_json, render


_SEVERITY = {"trace": 1, "debug": 5, "info": 9, "warn": 13, "error": 17, "fatal": 21}
"""@brief CLI 严重度到 OTel number 映射 / CLI severity-to-OTel-number mapping."""


def build_parser() -> argparse.ArgumentParser:
    """@brief 构造 Dashboard CLI / Build the Dashboard CLI.

    @return 参数解析器 / Argument parser.
    """

    parser = argparse.ArgumentParser(
        prog="fogmoe-dashboard",
        description="Explore FogMoe traces, logs, metrics, pipeline health, and Turn latency.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to the root JSONC configuration file.",
    )
    parser.add_argument(
        "--window", default="1h", help="Lookback such as 15m, 1h, or 7d."
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    subparsers = parser.add_subparsers(dest="view", metavar="view")

    subparsers.add_parser("overview", help="RED/USE overview and durable pipeline.")
    subparsers.add_parser("pipeline", help="Current durable workflow saturation.")

    spans = subparsers.add_parser("spans", help="Latency and error rate by operation.")
    spans.add_argument("--name")
    _add_limit(spans, 50)

    errors = subparsers.add_parser("errors", help="Merged span and log errors.")
    _add_limit(errors, 100)

    logs = subparsers.add_parser("logs", help="Structured logs.")
    logs.add_argument("--severity", choices=tuple(_SEVERITY), default="info")
    logs.add_argument("--logger")
    _add_limit(logs, 100)

    traces = subparsers.add_parser("traces", help="Trace summaries.")
    traces.add_argument("--errors-only", action="store_true")
    _add_limit(traces, 50)

    trace = subparsers.add_parser("trace", help="Trace waterfall and correlated logs.")
    trace.add_argument("trace_id")

    metrics = subparsers.add_parser("metrics", help="Metric window summaries.")
    metrics.add_argument("--name")
    _add_limit(metrics, 100)

    ai = subparsers.add_parser("ai", help="GenAI calls, latency, errors, and tokens.")
    _add_limit(ai, 50)

    subparsers.add_parser(
        "retrieval",
        help="Retrieval latency, embedding queues, outcomes, and saturation.",
    )

    latency = subparsers.add_parser(
        "latency", help="Turn stage latency and slow Turns."
    )
    _add_limit(latency, 50)

    resources = subparsers.add_parser("resources", help="Service instance lifecycles.")
    _add_limit(resources, 100)

    watch = subparsers.add_parser("watch", help="Live-refresh the overview.")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Stop after N refreshes; zero watches until interrupted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """@brief 运行 Dashboard CLI / Run the Dashboard CLI.

    @param argv 可替换命令参数 / Replaceable command arguments.
    @return None / None.
    """

    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    args.view = args.view or "overview"
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"fogmoe-dashboard: error: {error}\n")


async def _run(args: argparse.Namespace) -> None:
    """@brief 创建 client 并执行视图 / Create a client and execute a view."""

    duration = parse_duration(args.window)
    client = DashboardClient.from_database_settings(
        settings=read_dashboard_settings(args.config)
    )
    console = Console()
    async with client:
        if args.view == "watch":
            await _watch(client, console, args, duration)
            return
        window = TimeWindow.last(duration)
        query = _query(args, window)
        value = await execute_query(client, query)
        view = DashboardView(args.view)
        if args.format == "json":
            print_json(console, view, value)
        else:
            console.print(render(view, value))


def _query(
    args: argparse.Namespace,
    window: TimeWindow,
) -> DashboardQuery:
    """@brief 分派一个封闭视图集合 / Dispatch one closed set of views."""

    match args.view:
        case "overview":
            return OverviewQuery(window)
        case "pipeline":
            return PipelineQuery()
        case "spans":
            return SpansQuery(window, name=args.name, limit=args.limit)
        case "errors":
            return ErrorsQuery(window, limit=args.limit)
        case "logs":
            return LogsQuery(
                window,
                minimum_severity=_SEVERITY[args.severity],
                logger_name=args.logger,
                limit=args.limit,
            )
        case "traces":
            return TracesQuery(
                window,
                errors_only=args.errors_only,
                limit=args.limit,
            )
        case "trace":
            return TraceQuery(args.trace_id)
        case "metrics":
            return MetricsQuery(window, name=args.name, limit=args.limit)
        case "ai":
            return GenAiQuery(window, limit=args.limit)
        case "retrieval":
            return RetrievalQuery(window)
        case "latency":
            return LatencyQuery(window, slow_turn_limit=args.limit)
        case "resources":
            return ResourcesQuery(window, limit=args.limit)
        case _:
            raise ValueError(f"Unknown Dashboard view: {args.view}")


async def _watch(
    client: DashboardClient,
    console: Console,
    args: argparse.Namespace,
    duration: timedelta,
) -> None:
    """@brief Live 或 JSON Lines 刷新总览 / Refresh the overview as Live output or JSON Lines."""

    if args.interval < 0.5:
        raise ValueError("watch interval must be at least 0.5 seconds")
    if args.iterations < 0:
        raise ValueError("watch iterations cannot be negative")
    iterations = 0
    if args.format == "json":
        while args.iterations == 0 or iterations < args.iterations:
            value = await client.overview(TimeWindow.last(duration))
            print_json(console, DashboardView.OVERVIEW, value)
            iterations += 1
            if args.iterations == 0 or iterations < args.iterations:
                await asyncio.sleep(args.interval)
        return
    initial = await client.overview(TimeWindow.last(duration))
    with Live(
        render(DashboardView.OVERVIEW, initial), console=console, refresh_per_second=4
    ) as live:
        iterations = 1
        while args.iterations == 0 or iterations < args.iterations:
            await asyncio.sleep(args.interval)
            value = await client.overview(TimeWindow.last(duration))
            live.update(render(DashboardView.OVERVIEW, value), refresh=True)
            iterations += 1


def _add_limit(parser: argparse.ArgumentParser, default: int) -> None:
    """@brief 为视图增加统一 limit / Add a uniform limit to a view."""

    parser.add_argument("--limit", type=int, default=default)


__all__ = ["build_parser", "main"]
