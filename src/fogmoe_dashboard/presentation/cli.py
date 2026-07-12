"""@brief FogMoe Dashboard 交互式 CLI / Interactive FogMoe Dashboard CLI."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from rich.console import Console
from rich.live import Live

from fogmoe_dashboard.api import DashboardClient
from fogmoe_dashboard.config import DEFAULT_CONFIG_DIR
from fogmoe_dashboard.domain.models import TimeWindow
from fogmoe_dashboard.presentation.render import print_json, render


_DURATION = re.compile(r"(?P<amount>[0-9]+(?:\.[0-9]+)?)(?P<unit>[smhd])\Z")
"""@brief CLI 紧凑时长语法 / Compact CLI duration syntax."""
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
        "--database-url", help="Explicit PostgreSQL URL; overrides service files."
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--service", default="fogmoe_automation")
    parser.add_argument(
        "--window", default="1h", help="Lookback such as 15m, 1h, or 7d."
    )
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--timeout", type=float, default=5.0)
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
    client = _client(args)
    console = Console()
    async with client:
        if args.view == "watch":
            await _watch(client, console, args, duration)
            return
        window = TimeWindow.last(duration)
        value = await _query(client, args, window)
        if args.format == "json":
            print_json(console, args.view, value)
        else:
            console.print(render(args.view, value))


def _client(args: argparse.Namespace) -> DashboardClient:
    """@brief 从 CLI 配置装配 client / Compose a client from CLI configuration."""

    if args.database_url:
        return DashboardClient.from_database_url(
            args.database_url,
            command_timeout=args.timeout,
        )
    return DashboardClient.from_environment(
        config_dir=args.config_dir,
        service_name=args.service,
        command_timeout=args.timeout,
    )


async def _query(
    client: DashboardClient,
    args: argparse.Namespace,
    window: TimeWindow,
) -> object:
    """@brief 分派一个封闭视图集合 / Dispatch one closed set of views."""

    match args.view:
        case "overview":
            return await client.overview(window)
        case "pipeline":
            return await client.pipeline()
        case "spans":
            return await client.spans(window, name=args.name, limit=args.limit)
        case "errors":
            return await client.errors(window, limit=args.limit)
        case "logs":
            return await client.logs(
                window,
                minimum_severity=_SEVERITY[args.severity],
                logger_name=args.logger,
                limit=args.limit,
            )
        case "traces":
            return await client.traces(
                window,
                errors_only=args.errors_only,
                limit=args.limit,
            )
        case "trace":
            return await client.trace(args.trace_id)
        case "metrics":
            return await client.metrics(window, name=args.name, limit=args.limit)
        case "ai":
            return await client.gen_ai(window, limit=args.limit)
        case "latency":
            summary, slow_turns = await asyncio.gather(
                client.latency(window),
                client.slow_turns(window, limit=args.limit),
            )
            return {"summary": summary, "slow_turns": slow_turns}
        case "resources":
            return await client.resources(window, limit=args.limit)
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
            print_json(console, "overview", value)
            iterations += 1
            if args.iterations == 0 or iterations < args.iterations:
                await asyncio.sleep(args.interval)
        return
    initial = await client.overview(TimeWindow.last(duration))
    with Live(
        render("overview", initial), console=console, refresh_per_second=4
    ) as live:
        iterations = 1
        while args.iterations == 0 or iterations < args.iterations:
            await asyncio.sleep(args.interval)
            value = await client.overview(TimeWindow.last(duration))
            live.update(render("overview", value), refresh=True)
            iterations += 1


def parse_duration(value: str) -> timedelta:
    """@brief 解析紧凑正时长 / Parse a compact positive duration.

    @param value 例如 15m、1h、7d / Value such as 15m, 1h, or 7d.
    @return timedelta / timedelta.
    """

    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("window must look like 15m, 1h, or 7d")
    amount = float(match.group("amount"))
    factors = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    duration = timedelta(seconds=amount * factors[match.group("unit")])
    if duration <= timedelta():
        raise ValueError("window must be positive")
    return duration


def _add_limit(parser: argparse.ArgumentParser, default: int) -> None:
    """@brief 为视图增加统一 limit / Add a uniform limit to a view."""

    parser.add_argument("--limit", type=int, default=default)


__all__ = ["build_parser", "main", "parse_duration"]
