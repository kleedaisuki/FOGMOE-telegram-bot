"""@brief Dashboard 原生 Qt/Matplotlib 图表 / Native Qt/Matplotlib Dashboard charts."""

# mypy: disable-error-code="no-untyped-call"

from __future__ import annotations

from collections.abc import Sequence

from matplotlib.axes import Axes
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure

from fogmoe_dashboard.domain.models import (
    GenAiStats,
    HealthPoint,
    MetricStats,
    SpanStats,
    TraceDetail,
    TurnLatencyStats,
)


_BACKGROUND = "#111827"
"""@brief 图表背景色 / Chart background color."""
_FOREGROUND = "#d1d5db"
"""@brief 图表前景色 / Chart foreground color."""
_GRID = "#374151"
"""@brief 图表网格色 / Chart grid color."""
_CYAN = "#22d3ee"
"""@brief 主强调色 / Primary accent color."""
_VIOLET = "#a78bfa"
"""@brief 次强调色 / Secondary accent color."""
_RED = "#fb7185"
"""@brief 错误色 / Error color."""


class DashboardCanvas(FigureCanvasQTAgg):
    """@brief 共享视觉语法的 Matplotlib canvas / Matplotlib canvas with shared visual grammar."""

    def __init__(self, *, height: float = 3.2) -> None:
        """@brief 创建高 DPI 深色画布 / Create a high-DPI dark canvas."""

        self.figure = Figure(figsize=(8.0, height), layout="constrained")
        self.figure.set_facecolor(_BACKGROUND)
        super().__init__(self.figure)
        self.setMinimumHeight(int(height * 78))

    def _style(self, axis: Axes, *, title: str) -> None:
        """@brief 应用统一坐标轴样式 / Apply uniform axis styling."""

        axis.set_facecolor(_BACKGROUND)
        axis.set_title(title, color=_FOREGROUND, loc="left", fontsize=10)
        axis.tick_params(colors=_FOREGROUND, labelsize=8)
        axis.grid(True, color=_GRID, alpha=0.45, linewidth=0.7)
        for spine in axis.spines.values():
            spine.set_color(_GRID)
        axis.xaxis.label.set_color(_FOREGROUND)
        axis.yaxis.label.set_color(_FOREGROUND)

    def _empty(self, axis: Axes, message: str = "No data in this window") -> None:
        """@brief 呈现显式空状态 / Render an explicit empty state."""

        axis.text(
            0.5,
            0.5,
            message,
            color="#9ca3af",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
        axis.set_xticks([])
        axis.set_yticks([])


class HealthChart(DashboardCanvas):
    """@brief RED 健康趋势图 / RED health-trend chart."""

    def plot_points(self, points: Sequence[HealthPoint]) -> None:
        """@brief 绘制吞吐、错误、p95 与 token / Plot throughput, errors, p95, and tokens."""

        self.figure.clear()
        axes = self.figure.subplots(2, 2, sharex=True)
        rate_axis = axes[0, 0]
        errors_axis = axes[0, 1]
        latency_axis = axes[1, 0]
        tokens_axis = axes[1, 1]
        self._style(rate_axis, title="Throughput")
        self._style(errors_axis, title="Errors")
        self._style(latency_axis, title="p95 latency")
        self._style(tokens_axis, title="Token usage")
        if not points:
            self._empty(rate_axis)
            self._empty(errors_axis)
            self._empty(latency_axis)
            self._empty(tokens_axis)
            self.draw_idle()
            return
        times = [point.observed_at for point in points]
        rates = [point.span_rate_per_second for point in points]
        errors = [point.span_error_rate * 100 for point in points]
        error_logs = [point.error_logs for point in points]
        latency = [
            point.p95_ms if point.p95_ms is not None else float("nan")
            for point in points
        ]
        rate_axis.plot(times, rates, color=_CYAN, linewidth=1.8, label="span/s")
        rate_axis.fill_between(times, rates, color=_CYAN, alpha=0.12)
        error_axis = errors_axis.twinx()
        errors_axis.bar(
            times,
            error_logs,
            width=_datetime_bar_width(points),
            color="#7f1d1d",
            alpha=0.5,
            label="error logs",
        )
        error_axis.plot(times, errors, color=_RED, linewidth=1.2, label="error %")
        error_axis.tick_params(colors=_RED, labelsize=8)
        error_axis.set_ylabel("error %", color=_RED)
        rate_axis.set_ylabel("span/s")
        errors_axis.set_ylabel("error logs")
        latency_axis.plot(times, latency, color=_VIOLET, linewidth=1.8)
        latency_axis.fill_between(times, latency, color=_VIOLET, alpha=0.12)
        latency_axis.set_ylabel("ms")
        inputs = [point.input_tokens for point in points]
        outputs = [point.output_tokens for point in points]
        tokens_axis.stackplot(
            times,
            inputs,
            outputs,
            colors=(_CYAN, _VIOLET),
            alpha=0.65,
            labels=("input", "output"),
        )
        tokens_axis.set_ylabel("tokens")
        for axis in (latency_axis, tokens_axis):
            locator = AutoDateLocator(minticks=3, maxticks=6)
            axis.xaxis.set_major_locator(locator)
            axis.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        self.draw_idle()


class SpanLatencyChart(DashboardCanvas):
    """@brief 操作延迟分位数比较图 / Operation-latency percentile comparison chart."""

    def plot_spans(self, spans: Sequence[SpanStats]) -> None:
        """@brief 绘制最慢操作的 p50/p95/p99 / Plot p50/p95/p99 for the slowest operations."""

        self.figure.clear()
        axis = self.figure.subplots()
        self._style(axis, title="Slowest operations · latency percentiles")
        selected = tuple(spans[:12])[::-1]
        if not selected:
            self._empty(axis)
            self.draw_idle()
            return
        positions = list(range(len(selected)))
        axis.barh(
            positions, [item.p99_ms for item in selected], color="#4c1d95", label="p99"
        )
        axis.barh(
            positions, [item.p95_ms for item in selected], color=_VIOLET, label="p95"
        )
        axis.barh(
            positions, [item.p50_ms for item in selected], color=_CYAN, label="p50"
        )
        axis.set_yticks(positions, [item.name for item in selected])
        axis.set_xlabel("milliseconds")
        legend = axis.legend(frameon=False, fontsize=8)
        for text in legend.get_texts():
            text.set_color(_FOREGROUND)
        self.draw_idle()


class MetricRangeChart(DashboardCanvas):
    """@brief Metric 当前值与范围图 / Metric current-value and range chart."""

    def plot_metrics(self, metrics: Sequence[MetricStats]) -> None:
        """@brief 绘制 latest、average 与 min/max 范围 / Plot latest, average, and min/max ranges."""

        self.figure.clear()
        axis = self.figure.subplots()
        self._style(axis, title="Metric range · latest / average / min–max")
        selected = tuple(metrics[:14])[::-1]
        if not selected:
            self._empty(axis)
            self.draw_idle()
            return
        positions = list(range(len(selected)))
        for position, item in zip(positions, selected, strict=True):
            axis.plot(
                [item.minimum, item.maximum],
                [position, position],
                color="#64748b",
                linewidth=4,
            )
        axis.scatter(
            [item.average for item in selected],
            positions,
            color=_VIOLET,
            s=24,
            label="average",
        )
        axis.scatter(
            [item.latest for item in selected],
            positions,
            color=_CYAN,
            s=28,
            label="latest",
            marker="D",
        )
        axis.set_yticks(positions, [item.name for item in selected])
        legend = axis.legend(frameon=False, fontsize=8)
        for text in legend.get_texts():
            text.set_color(_FOREGROUND)
        self.draw_idle()


class GenAiChart(DashboardCanvas):
    """@brief Provider/model token 与延迟图 / Provider/model token and latency chart."""

    def plot_usage(self, rows: Sequence[GenAiStats]) -> None:
        """@brief 绘制 token 堆叠柱和 p95 延迟 / Plot stacked tokens and p95 latency."""

        self.figure.clear()
        token_axis, latency_axis = self.figure.subplots(1, 2)
        self._style(token_axis, title="Token usage")
        self._style(latency_axis, title="p95 inference latency")
        selected = tuple(rows[:10])
        if not selected:
            self._empty(token_axis)
            self._empty(latency_axis)
            self.draw_idle()
            return
        labels = [f"{item.provider}\n{item.model}" for item in selected]
        positions = list(range(len(selected)))
        inputs = [item.input_tokens for item in selected]
        token_axis.bar(positions, inputs, color=_CYAN, label="input")
        token_axis.bar(
            positions,
            [item.output_tokens for item in selected],
            bottom=inputs,
            color=_VIOLET,
            label="output",
        )
        token_axis.set_xticks(positions, labels, rotation=30, ha="right")
        latency_axis.bar(positions, [item.p95_ms for item in selected], color=_VIOLET)
        latency_axis.set_xticks(positions, labels, rotation=30, ha="right")
        latency_axis.set_ylabel("ms")
        self.draw_idle()


class TurnLatencyChart(DashboardCanvas):
    """@brief Turn 各阶段延迟图 / Turn stage-latency chart."""

    def plot_latency(self, rows: Sequence[TurnLatencyStats]) -> None:
        """@brief 按状态比较端到端、推理与投递 p95 / Compare end-to-end, inference, and delivery p95 by state."""

        self.figure.clear()
        axis = self.figure.subplots()
        self._style(axis, title="Turn p95 latency by state")
        if not rows:
            self._empty(axis)
            self.draw_idle()
            return
        positions = list(range(len(rows)))
        width = 0.24
        axis.bar(
            [position - width for position in positions],
            [_zero(item.p95_end_to_end_ms) for item in rows],
            width,
            color=_CYAN,
            label="end-to-end",
        )
        axis.bar(
            positions,
            [_zero(item.p95_inference_ms) for item in rows],
            width,
            color=_VIOLET,
            label="inference",
        )
        axis.bar(
            [position + width for position in positions],
            [_zero(item.p95_delivery_ms) for item in rows],
            width,
            color="#fbbf24",
            label="delivery",
        )
        axis.set_xticks(positions, [item.state for item in rows])
        axis.set_ylabel("ms")
        legend = axis.legend(frameon=False, fontsize=8)
        for text in legend.get_texts():
            text.set_color(_FOREGROUND)
        self.draw_idle()


class TraceWaterfallChart(DashboardCanvas):
    """@brief Trace 关键路径 waterfall / Trace critical-path waterfall."""

    def plot_trace(self, trace: TraceDetail | None) -> None:
        """@brief 绘制 span 相对开始与持续时间 / Plot relative span starts and durations."""

        self.figure.clear()
        axis = self.figure.subplots()
        self._style(axis, title="Trace waterfall")
        if trace is None or not trace.spans:
            self._empty(axis, "Select a trace to inspect its waterfall")
            self.draw_idle()
            return
        origin = min(span.started_at for span in trace.spans)
        spans = tuple(trace.spans)[::-1]
        positions = list(range(len(spans)))
        starts = [(span.started_at - origin).total_seconds() * 1000 for span in spans]
        colors = [_RED if span.status == "error" else _CYAN for span in spans]
        axis.barh(
            positions,
            [span.duration_ms for span in spans],
            left=starts,
            color=colors,
            height=0.62,
        )
        axis.set_yticks(
            positions, [_span_label(span.name, span.span_id) for span in spans]
        )
        axis.set_xlabel("milliseconds from trace start")
        self.draw_idle()


def _zero(value: float | None) -> float:
    """@brief 将缺失图表值映射为零 / Map a missing chart value to zero."""

    return 0.0 if value is None else value


def _span_label(name: str, span_id: str) -> str:
    """@brief 构造可区分的 span 标签 / Build a distinguishable span label."""

    return f"{name} · {span_id[:6]}"


def _datetime_bar_width(points: Sequence[HealthPoint]) -> float:
    """@brief 返回 Matplotlib 日期单位中的安全柱宽 / Return a safe bar width in Matplotlib date units."""

    if len(points) < 2:
        return 1.0 / 24.0
    seconds = (points[1].observed_at - points[0].observed_at).total_seconds()
    return max(1.0 / 86400.0, seconds / 86400.0 * 0.8)


__all__ = [
    "GenAiChart",
    "HealthChart",
    "MetricRangeChart",
    "SpanLatencyChart",
    "TraceWaterfallChart",
    "TurnLatencyChart",
]
