"""@brief Rich 与 JSON 视图渲染 / Rich and JSON view rendering."""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fogmoe_dashboard.domain.models import Overview, TraceDetail


_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "pipeline": (
        ("stage", "Stage"),
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("retrying", "Retry"),
        ("failed_final", "Failed"),
        ("expired_leases", "Expired leases"),
        ("oldest_ready_at", "Oldest ready"),
    ),
    "spans": (
        ("name", "Operation"),
        ("kind", "Kind"),
        ("calls", "Calls"),
        ("rate_per_second", "Rate/s"),
        ("errors", "Errors"),
        ("error_rate", "Error %"),
        ("p50_ms", "p50 ms"),
        ("p95_ms", "p95 ms"),
        ("p99_ms", "p99 ms"),
        ("maximum_ms", "Max ms"),
    ),
    "errors": (
        ("occurred_at", "Time"),
        ("source", "Source"),
        ("name", "Name"),
        ("message", "Message"),
        ("trace_id", "Trace"),
        ("turn_id", "Turn"),
    ),
    "logs": (
        ("occurred_at", "Time"),
        ("severity_text", "Level"),
        ("logger_name", "Logger"),
        ("event_name", "Event"),
        ("body", "Body"),
        ("trace_id", "Trace"),
    ),
    "traces": (
        ("started_at", "Started"),
        ("trace_id", "Trace"),
        ("root_operations", "Roots"),
        ("span_count", "Spans"),
        ("error_count", "Errors"),
        ("duration_ms", "Duration ms"),
    ),
    "metrics": (
        ("name", "Metric"),
        ("kind", "Kind"),
        ("unit", "Unit"),
        ("points", "Points"),
        ("latest", "Latest"),
        ("minimum", "Min"),
        ("maximum", "Max"),
        ("average", "Average"),
        ("total", "Total"),
        ("rate_per_second", "Rate/s"),
        ("latest_at", "Latest at"),
    ),
    "ai": (
        ("provider", "Provider"),
        ("model", "Model"),
        ("calls", "Calls"),
        ("errors", "Errors"),
        ("input_tokens", "Input tokens"),
        ("output_tokens", "Output tokens"),
        ("p50_ms", "p50 ms"),
        ("p95_ms", "p95 ms"),
    ),
    "latency": (
        ("state", "State"),
        ("turns", "Turns"),
        ("p50_end_to_end_ms", "E2E p50 ms"),
        ("p95_end_to_end_ms", "E2E p95 ms"),
        ("p95_inference_ms", "Inference p95"),
        ("p95_delivery_ms", "Delivery p95"),
        ("average_inference_attempts", "Inference tries"),
        ("average_delivery_attempts", "Delivery tries"),
    ),
    "slow_turns": (
        ("turn_id", "Turn"),
        ("state", "State"),
        ("end_to_end_ms", "E2E ms"),
        ("inference_ms", "Inference ms"),
        ("delivery_ms", "Delivery ms"),
        ("inference_attempts", "Inference tries"),
        ("delivery_attempts", "Delivery tries"),
    ),
    "resources": (
        ("active", "Active"),
        ("service_name", "Service"),
        ("service_version", "Version"),
        ("environment", "Environment"),
        ("instance_id", "Instance"),
        ("started_at", "Started"),
        ("stopped_at", "Stopped"),
    ),
}
"""@brief 低噪声终端列定义 / Low-noise terminal column definitions."""


def print_json(console: Console, view: str, value: object) -> None:
    """@brief 输出稳定 JSON envelope / Print a stable JSON envelope.

    @param console 输出 console / Output console.
    @param view 视图名 / View name.
    @param value 类型化结果 / Typed result.
    @return None / None.
    """

    payload = {"schema_version": 1, "view": view, "data": to_jsonable(value)}
    console.print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def render(view: str, value: object) -> RenderableType:
    """@brief 将查询结果映射为 Rich renderable / Map a query result to a Rich renderable.

    @param view 视图名 / View name.
    @param value 类型化结果 / Typed result.
    @return Rich renderable / Rich renderable.
    """

    if view == "overview":
        if not isinstance(value, Overview):
            raise TypeError("overview renderer requires Overview")
        return _overview(value)
    if view == "trace":
        if not isinstance(value, TraceDetail):
            raise TypeError("trace renderer requires TraceDetail")
        return _trace(value)
    if view == "latency":
        if not isinstance(value, dict):
            raise TypeError("latency renderer requires a mapping")
        return Group(
            _table("latency", value.get("summary", ()), title="Turn latency"),
            _table("slow_turns", value.get("slow_turns", ()), title="Slow Turns"),
        )
    return _table(view, value, title=view.replace("_", " ").title())


def to_jsonable(value: object) -> object:
    """@brief 递归转换公开模型为 JSON 值 / Recursively convert public models to JSON values.

    @param value 任意公开返回值 / Any public return value.
    @return JSON 可编码值 / JSON-encodable value.
    """

    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID | Enum):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [to_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    raise TypeError(f"Unsupported Dashboard JSON value: {type(value).__name__}")


def _overview(value: Overview) -> RenderableType:
    """@brief 构造总览 panels / Build overview panels."""

    summary = Table.grid(expand=True)
    for _ in range(4):
        summary.add_column(justify="center", ratio=1)
    summary.add_row(
        _stat("Spans", str(value.spans)),
        _stat("Error rate", f"{value.span_error_rate:.2%}"),
        _stat("Traces", str(value.traces)),
        _stat("Error logs", str(value.error_logs)),
    )
    summary.add_row(
        _stat("p50", _milliseconds(value.p50_ms)),
        _stat("p95", _milliseconds(value.p95_ms)),
        _stat("Input tokens", f"{value.input_tokens:,}"),
        _stat("Output tokens", f"{value.output_tokens:,}"),
    )
    title = f"FogMoe observability · {value.window.start:%Y-%m-%d %H:%M} → {value.window.end:%H:%M} UTC"
    return Group(
        Panel(summary, title=title, border_style="cyan"),
        _table("pipeline", value.pipeline, title="Durable pipeline"),
    )


def _trace(value: TraceDetail) -> RenderableType:
    """@brief 构造 trace waterfall 与关联日志 / Build trace waterfall and correlated logs."""

    span_table = Table(
        title=f"Trace {value.trace_id}",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    for heading in ("Operation", "Kind", "Status", "Start", "Duration", "Span"):
        span_table.add_column(
            heading, overflow="fold" if heading == "Operation" else "ellipsis"
        )
    parents = {span.span_id: span.parent_span_id for span in value.spans}
    for span in value.spans:
        depth = _depth(span.span_id, parents)
        status = "[red]error[/red]" if span.status == "error" else span.status
        span_table.add_row(
            f"{'  ' * depth}{'└─ ' if depth else ''}{span.name}",
            span.kind,
            status,
            span.started_at.strftime("%H:%M:%S.%f")[:-3],
            _milliseconds(span.duration_ms),
            span.span_id,
        )
    log_table = Table(title="Correlated logs", box=box.SIMPLE, expand=True)
    for heading in ("Time", "Level", "Logger", "Event", "Body"):
        log_table.add_column(heading, overflow="fold")
    for log in value.logs:
        log_table.add_row(
            log.occurred_at.strftime("%H:%M:%S.%f")[:-3],
            log.severity_text,
            log.logger_name,
            log.event_name or "—",
            log.body,
        )
    return Group(span_table, log_table)


def _table(view: str, value: object, *, title: str) -> Table:
    """@brief 构造统一表格 / Build a uniform table."""

    columns = _COLUMNS.get(view)
    if columns is None:
        raise ValueError(f"No table definition for view {view!r}")
    rows = value if isinstance(value, tuple | list) else ()
    table = Table(title=title, box=box.SIMPLE_HEAVY, expand=True)
    for _, heading in columns:
        table.add_column(heading, overflow="fold")
    for row in rows:
        table.add_row(*(_format_cell(name, getattr(row, name)) for name, _ in columns))
    return table


def _format_cell(name: str, value: object) -> str:
    """@brief 格式化终端 cell / Format a terminal cell."""

    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, bool):
        return "[green]yes[/green]" if value else "[dim]no[/dim]"
    if name == "error_rate" and isinstance(value, float):
        return f"{value:.2%}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    text = str(value)
    if name == "trace_id" and len(text) == 32:
        return text
    return text


def _stat(label: str, value: str) -> Text:
    """@brief 构造紧凑统计格 / Build a compact statistic cell."""

    return Text.assemble((label + "\n", "dim"), (value, "bold"), justify="center")


def _milliseconds(value: float | None) -> str:
    """@brief 格式化毫秒 / Format milliseconds."""

    return "—" if value is None else f"{value:,.2f} ms"


def _depth(span_id: str, parents: dict[str, str | None]) -> int:
    """@brief 计算无环父链深度 / Calculate acyclic parent-chain depth."""

    depth = 0
    seen = {span_id}
    parent = parents.get(span_id)
    while parent is not None and parent not in seen and depth < 32:
        seen.add(parent)
        depth += 1
        parent = parents.get(parent)
    return depth


__all__ = ["print_json", "render", "to_jsonable"]
