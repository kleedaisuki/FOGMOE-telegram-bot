"""@brief FogMoe 可观测性 GUI 页面 / FogMoe observability GUI pages."""

from __future__ import annotations

import json
from typing import cast

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fogmoe_dashboard.application.queries import (
    DashboardQuery,
    DashboardResult,
    ErrorsQuery,
    GenAiQuery,
    HealthSeriesQuery,
    LatencyQuery,
    LogsQuery,
    MetricsQuery,
    OverviewQuery,
    ResourcesQuery,
    SpansQuery,
    TraceQuery,
    TracesQuery,
)
from fogmoe_dashboard.domain.models import (
    ErrorEvent,
    GenAiStats,
    HealthPoint,
    LatencySnapshot,
    LogEntry,
    MetricStats,
    Overview,
    PipelineStage,
    ResourceInstance,
    SlowTurn,
    SpanStats,
    TimeWindow,
    TraceDetail,
    TraceLog,
    TraceSummary,
    TurnLatencyStats,
)
from fogmoe_dashboard.presentation.gui.base import (
    KpiCard,
    QueryPage,
    page_header,
    table_view,
)
from fogmoe_dashboard.presentation.gui.charts import (
    GenAiChart,
    HealthChart,
    MetricRangeChart,
    SpanLatencyChart,
    TraceWaterfallChart,
    TurnLatencyChart,
)
from fogmoe_dashboard.presentation.gui.table import (
    ObjectTableModel,
    TableColumn,
    integer,
    milliseconds,
    percent,
)


_RIGHT = Qt.AlignmentFlag.AlignRight
"""@brief 数值列右对齐 / Right alignment for numeric columns."""
_SEVERITIES = (
    ("TRACE", 1),
    ("DEBUG", 5),
    ("INFO", 9),
    ("WARN", 13),
    ("ERROR", 17),
    ("FATAL", 21),
)
"""@brief OpenTelemetry 严重度筛选项 / OpenTelemetry severity filter choices."""


class OverviewPage(QueryPage):
    """@brief RED/USE 总览与 durable pipeline 页面 / RED/USE overview and durable-pipeline page."""

    def __init__(self) -> None:
        """@brief 组装指标、趋势与 pipeline / Compose KPIs, trends, and pipeline."""

        super().__init__()
        self._cards = {
            "spans": KpiCard("Spans"),
            "error_rate": KpiCard("Span 错误率"),
            "traces": KpiCard("Traces"),
            "p95": KpiCard("p95 延迟"),
            "logs": KpiCard("Logs"),
            "error_logs": KpiCard("错误日志"),
            "tokens": KpiCard("Tokens"),
            "tool_calls": KpiCard("Tool calls"),
        }
        card_grid = QGridLayout()
        card_grid.setContentsMargins(0, 0, 0, 0)
        for index, card in enumerate(self._cards.values()):
            card_grid.addWidget(card, index // 4, index % 4)
        self._chart = HealthChart(height=3.6)
        self._pipeline_model = ObjectTableModel[PipelineStage](
            (
                TableColumn("阶段", lambda row: row.stage),
                TableColumn("等待", lambda row: row.pending, integer, _RIGHT),
                TableColumn("处理中", lambda row: row.processing, integer, _RIGHT),
                TableColumn("重试", lambda row: row.retrying, integer, _RIGHT),
                TableColumn("最终失败", lambda row: row.failed_final, integer, _RIGHT),
                TableColumn(
                    "过期 lease", lambda row: row.expired_leases, integer, _RIGHT
                ),
                TableColumn("最早就绪", lambda row: row.oldest_ready_at, stretch=True),
            )
        )
        self._pipeline = table_view(self._pipeline_model)
        self._pipeline.setMaximumHeight(180)
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "系统总览",
                "先看吞吐、错误与延迟趋势，再检查 durable pipeline 是否形成积压。",
            )
        )
        layout.addLayout(card_grid)
        layout.addWidget(self._chart, stretch=1)
        layout.addWidget(QLabel("Durable pipeline"))
        layout.addWidget(self._pipeline)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 请求同窗总览与健康趋势 / Request overview and health trends for the same window."""

        return OverviewQuery(window), HealthSeriesQuery(window)

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 原子更新对应区域 / Atomically update the corresponding region."""

        if isinstance(query, OverviewQuery) and isinstance(value, Overview):
            self._show_overview(value)
        elif isinstance(query, HealthSeriesQuery):
            self._chart.plot_points(cast(tuple[HealthPoint, ...], value))

    def _show_overview(self, value: Overview) -> None:
        """@brief 映射总览领域语义到 KPI / Map overview domain semantics to KPIs."""

        self._cards["spans"].set_value(f"{value.spans:,}")
        self._cards["error_rate"].set_value(
            f"{value.span_error_rate:.2%}", alert=value.span_error_rate > 0
        )
        self._cards["traces"].set_value(f"{value.traces:,}")
        self._cards["p95"].set_value(milliseconds(value.p95_ms))
        self._cards["logs"].set_value(f"{value.logs:,}")
        self._cards["error_logs"].set_value(
            f"{value.error_logs:,}", alert=value.error_logs > 0
        )
        self._cards["tokens"].set_value(f"{value.input_tokens + value.output_tokens:,}")
        self._cards["tool_calls"].set_value(f"{value.tool_calls:,}")
        self._pipeline_model.replace(value.pipeline)


class OperationsPage(QueryPage):
    """@brief Span RED 操作分析页面 / Span RED operation-analysis page."""

    def __init__(self) -> None:
        """@brief 组装精确筛选、延迟图与明细表 / Compose exact filter, latency chart, and detail table."""

        super().__init__()
        self._name = QLineEdit()
        self._name.setPlaceholderText("精确 span name；留空查看全部")
        apply_button = QPushButton("应用筛选")
        apply_button.clicked.connect(self.refresh_requested)
        self._name.returnPressed.connect(self.refresh_requested)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Operation"))
        filters.addWidget(self._name, stretch=1)
        filters.addWidget(apply_button)
        self._chart = SpanLatencyChart()
        self._model = ObjectTableModel[SpanStats](
            (
                TableColumn("Operation", lambda row: row.name, stretch=True),
                TableColumn("Kind", lambda row: row.kind),
                TableColumn("Calls", lambda row: row.calls, integer, _RIGHT),
                TableColumn(
                    "Rate/s", lambda row: row.rate_per_second, alignment=_RIGHT
                ),
                TableColumn("错误", lambda row: row.errors, integer, _RIGHT),
                TableColumn("错误率", lambda row: row.error_rate, percent, _RIGHT),
                TableColumn("p50", lambda row: row.p50_ms, milliseconds, _RIGHT),
                TableColumn("p95", lambda row: row.p95_ms, milliseconds, _RIGHT),
                TableColumn("p99", lambda row: row.p99_ms, milliseconds, _RIGHT),
                TableColumn("Max", lambda row: row.maximum_ms, milliseconds, _RIGHT),
            )
        )
        self._table = table_view(self._model)
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "操作分析",
                "按 RED（Rate、Errors、Duration）排序热点；精确筛选可收敛到单个 operation。",
            )
        )
        layout.addLayout(filters)
        layout.addWidget(self._chart, stretch=1)
        layout.addWidget(self._table, stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 返回当前 Span 查询 / Return the current Span query."""

        name = self._name.text().strip() or None
        return (SpansQuery(window, name=name, limit=200),)

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新 Span 图表与表格 / Update the Span chart and table."""

        if isinstance(query, SpansQuery):
            rows = cast(tuple[SpanStats, ...], value)
            self._model.replace(rows)
            self._chart.plot_spans(rows)


class EventsPage(QueryPage):
    """@brief 错误流与结构日志关联分析页面 / Correlated error-stream and structured-log page."""

    def __init__(self) -> None:
        """@brief 组装日志筛选和双事件表 / Compose log filters and two event tables."""

        super().__init__()
        self._severity = QComboBox()
        for label, number in _SEVERITIES:
            self._severity.addItem(label, number)
        self._severity.setCurrentText("INFO")
        self._logger = QLineEdit()
        self._logger.setPlaceholderText("精确 logger name；留空查看全部")
        apply_button = QPushButton("应用筛选")
        apply_button.clicked.connect(self.refresh_requested)
        self._logger.returnPressed.connect(self.refresh_requested)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("最低级别"))
        filters.addWidget(self._severity)
        filters.addWidget(QLabel("Logger"))
        filters.addWidget(self._logger, stretch=1)
        filters.addWidget(apply_button)
        self._error_model = ObjectTableModel[ErrorEvent](
            (
                TableColumn("时间", lambda row: row.occurred_at),
                TableColumn("来源", lambda row: row.source),
                TableColumn("名称", lambda row: row.name),
                TableColumn("消息", lambda row: row.message, stretch=True),
                TableColumn("Trace", lambda row: row.trace_id),
                TableColumn("Turn", lambda row: row.turn_id),
            )
        )
        self._log_model = ObjectTableModel[LogEntry](
            (
                TableColumn("时间", lambda row: row.occurred_at),
                TableColumn("级别", lambda row: row.severity_text),
                TableColumn("Logger", lambda row: row.logger_name),
                TableColumn("Event", lambda row: row.event_name),
                TableColumn("Body", lambda row: row.body, stretch=True),
                TableColumn("Trace", lambda row: row.trace_id),
            )
        )
        self._errors = table_view(self._error_model)
        self._logs = table_view(self._log_model)
        self._errors.doubleClicked.connect(self._open_error_trace)
        self._logs.doubleClicked.connect(self._open_log_trace)
        tabs = QTabWidget()
        tabs.addTab(self._errors, "错误")
        tabs.addTab(self._logs, "全部日志")
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "事件与日志",
                "错误 span 与 error log 合并排序；双击带 trace 的事件可直接下钻。",
            )
        )
        layout.addLayout(filters)
        layout.addWidget(tabs, stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 同窗请求错误与日志 / Request errors and logs for the same window."""

        return (
            ErrorsQuery(window, limit=300),
            LogsQuery(
                window,
                minimum_severity=int(self._severity.currentData()),
                logger_name=self._logger.text().strip() or None,
                limit=500,
            ),
        )

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新对应事件表 / Update the corresponding event table."""

        if isinstance(query, ErrorsQuery):
            self._error_model.replace(cast(tuple[ErrorEvent, ...], value))
        elif isinstance(query, LogsQuery):
            self._log_model.replace(cast(tuple[LogEntry, ...], value))

    def _open_error_trace(self, index: QModelIndex) -> None:
        """@brief 从错误行请求 trace / Request a trace from an error row."""

        row = self._error_model.item(index.row())
        if row is not None and row.trace_id is not None:
            self.query_requested.emit(TraceQuery(row.trace_id))

    def _open_log_trace(self, index: QModelIndex) -> None:
        """@brief 从日志行请求 trace / Request a trace from a log row."""

        row = self._log_model.item(index.row())
        if row is not None and row.trace_id is not None:
            self.query_requested.emit(TraceQuery(row.trace_id))


class TracesPage(QueryPage):
    """@brief Trace 搜索、waterfall 与上下文 drill-down 页面 / Trace search, waterfall, and contextual drill-down page."""

    def __init__(self) -> None:
        """@brief 组装 trace master-detail 视图 / Compose the trace master-detail view."""

        super().__init__()
        self._errors_only = QCheckBox("仅含错误")
        self._errors_only.toggled.connect(self.refresh_requested)
        self._trace_id = QLineEdit()
        self._trace_id.setPlaceholderText("输入 32 位 trace id 后回车")
        self._trace_id.returnPressed.connect(self._request_explicit_trace)
        controls = QHBoxLayout()
        controls.addWidget(self._errors_only)
        controls.addWidget(self._trace_id, stretch=1)
        self._summary_model = ObjectTableModel[TraceSummary](
            (
                TableColumn("开始", lambda row: row.started_at),
                TableColumn("Trace", lambda row: row.trace_id),
                TableColumn(
                    "Root operations", lambda row: row.root_operations, stretch=True
                ),
                TableColumn("Spans", lambda row: row.span_count, integer, _RIGHT),
                TableColumn("错误", lambda row: row.error_count, integer, _RIGHT),
                TableColumn(
                    "Duration", lambda row: row.duration_ms, milliseconds, _RIGHT
                ),
            )
        )
        self._summaries = table_view(self._summary_model)
        self._summaries.doubleClicked.connect(self._open_summary)
        self._waterfall = TraceWaterfallChart(height=3.8)
        self._log_model = ObjectTableModel[TraceLog](
            (
                TableColumn("时间", lambda row: row.occurred_at),
                TableColumn("级别", lambda row: row.severity_text),
                TableColumn("Logger", lambda row: row.logger_name),
                TableColumn("Event", lambda row: row.event_name),
                TableColumn("Body", lambda row: row.body, stretch=True),
            )
        )
        self._logs = table_view(self._log_model)
        self._attributes = QPlainTextEdit()
        self._attributes.setReadOnly(True)
        self._attributes.setPlaceholderText("Span attributes 会在选中 trace 后显示")
        detail_tabs = QTabWidget()
        detail_tabs.addTab(self._logs, "关联日志")
        detail_tabs.addTab(self._attributes, "Span attributes")
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self._waterfall, stretch=2)
        detail_layout.addWidget(detail_tabs, stretch=1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._summaries)
        splitter.addWidget(detail)
        splitter.setSizes([260, 520])
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Distributed traces",
                "先按摘要定位异常请求，再用 waterfall、日志和 attributes 重建执行上下文。",
            )
        )
        layout.addLayout(controls)
        layout.addWidget(splitter, stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 返回当前 Trace 摘要查询 / Return the current Trace-summary query."""

        return (
            TracesQuery(window, errors_only=self._errors_only.isChecked(), limit=250),
        )

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新摘要或 drill-down / Update summaries or drill-down detail."""

        if isinstance(query, TracesQuery):
            self._summary_model.replace(cast(tuple[TraceSummary, ...], value))
        elif isinstance(query, TraceQuery) and isinstance(value, TraceDetail):
            self._trace_id.setText(value.trace_id)
            self._waterfall.plot_trace(value)
            self._log_model.replace(value.logs)
            attributes = {
                f"{span.name} · {span.span_id}": span.attributes for span in value.spans
            }
            self._attributes.setPlainText(
                json.dumps(attributes, ensure_ascii=False, indent=2, sort_keys=True)
            )

    def _open_summary(self, index: QModelIndex) -> None:
        """@brief 从摘要行请求完整 trace / Request a complete trace from a summary row."""

        row = self._summary_model.item(index.row())
        if row is not None:
            self.query_requested.emit(TraceQuery(row.trace_id))

    def _request_explicit_trace(self) -> None:
        """@brief 请求文本框中的 trace / Request the trace entered in the text box."""

        trace_id = self._trace_id.text().strip()
        if trace_id:
            self.query_requested.emit(TraceQuery(trace_id))


class MetricsPage(QueryPage):
    """@brief Metric 摘要与范围分析页面 / Metric-summary and range-analysis page."""

    def __init__(self) -> None:
        """@brief 组装 metric 筛选、范围图与表格 / Compose metric filter, range chart, and table."""

        super().__init__()
        self._name = QLineEdit()
        self._name.setPlaceholderText("精确 metric name；留空查看全部")
        apply_button = QPushButton("应用筛选")
        apply_button.clicked.connect(self.refresh_requested)
        self._name.returnPressed.connect(self.refresh_requested)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Metric"))
        filters.addWidget(self._name, stretch=1)
        filters.addWidget(apply_button)
        self._chart = MetricRangeChart()
        self._model = ObjectTableModel[MetricStats](
            (
                TableColumn("Metric", lambda row: row.name, stretch=True),
                TableColumn("Kind", lambda row: row.kind),
                TableColumn("Unit", lambda row: row.unit),
                TableColumn("Points", lambda row: row.points, integer, _RIGHT),
                TableColumn("Latest", lambda row: row.latest, alignment=_RIGHT),
                TableColumn("Min", lambda row: row.minimum, alignment=_RIGHT),
                TableColumn("Average", lambda row: row.average, alignment=_RIGHT),
                TableColumn("Max", lambda row: row.maximum, alignment=_RIGHT),
                TableColumn("Total", lambda row: row.total, alignment=_RIGHT),
                TableColumn(
                    "Rate/s", lambda row: row.rate_per_second, alignment=_RIGHT
                ),
            )
        )
        self._table = table_view(self._model)
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Metrics",
                "比较 latest/average/min–max；Counter 同时展示窗口 total 与 rate/s。",
            )
        )
        layout.addLayout(filters)
        layout.addWidget(self._chart, stretch=1)
        layout.addWidget(self._table, stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 返回当前 Metric 查询 / Return the current Metric query."""

        return (
            MetricsQuery(window, name=self._name.text().strip() or None, limit=300),
        )

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新 Metric 图表与表格 / Update the Metric chart and table."""

        if isinstance(query, MetricsQuery):
            rows = cast(tuple[MetricStats, ...], value)
            self._model.replace(rows)
            self._chart.plot_metrics(rows)


class AiTurnsPage(QueryPage):
    """@brief GenAI 使用与 Turn latency 联合页面 / Combined GenAI-usage and Turn-latency page."""

    def __init__(self) -> None:
        """@brief 组装两个业务视角的标签页 / Compose tabs for two business perspectives."""

        super().__init__()
        self._ai_chart = GenAiChart()
        self._ai_model = ObjectTableModel[GenAiStats](
            (
                TableColumn("Provider", lambda row: row.provider),
                TableColumn("Model", lambda row: row.model, stretch=True),
                TableColumn("Calls", lambda row: row.calls, integer, _RIGHT),
                TableColumn("错误", lambda row: row.errors, integer, _RIGHT),
                TableColumn(
                    "Input tokens", lambda row: row.input_tokens, integer, _RIGHT
                ),
                TableColumn(
                    "Output tokens", lambda row: row.output_tokens, integer, _RIGHT
                ),
                TableColumn("p50", lambda row: row.p50_ms, milliseconds, _RIGHT),
                TableColumn("p95", lambda row: row.p95_ms, milliseconds, _RIGHT),
            )
        )
        ai_widget = QWidget()
        ai_layout = QVBoxLayout(ai_widget)
        ai_layout.addWidget(self._ai_chart, stretch=1)
        ai_layout.addWidget(table_view(self._ai_model), stretch=1)
        self._latency_chart = TurnLatencyChart()
        self._latency_model = ObjectTableModel[TurnLatencyStats](
            (
                TableColumn("State", lambda row: row.state),
                TableColumn("Turns", lambda row: row.turns, integer, _RIGHT),
                TableColumn(
                    "E2E p50", lambda row: row.p50_end_to_end_ms, milliseconds, _RIGHT
                ),
                TableColumn(
                    "E2E p95", lambda row: row.p95_end_to_end_ms, milliseconds, _RIGHT
                ),
                TableColumn(
                    "Inference p95",
                    lambda row: row.p95_inference_ms,
                    milliseconds,
                    _RIGHT,
                ),
                TableColumn(
                    "Delivery p95",
                    lambda row: row.p95_delivery_ms,
                    milliseconds,
                    _RIGHT,
                ),
            )
        )
        self._slow_model = ObjectTableModel[SlowTurn](
            (
                TableColumn("Turn", lambda row: row.turn_id),
                TableColumn("State", lambda row: row.state),
                TableColumn("E2E", lambda row: row.end_to_end_ms, milliseconds, _RIGHT),
                TableColumn(
                    "Inference", lambda row: row.inference_ms, milliseconds, _RIGHT
                ),
                TableColumn(
                    "Delivery", lambda row: row.delivery_ms, milliseconds, _RIGHT
                ),
                TableColumn(
                    "Inference tries",
                    lambda row: row.inference_attempts,
                    integer,
                    _RIGHT,
                ),
                TableColumn(
                    "Delivery tries", lambda row: row.delivery_attempts, integer, _RIGHT
                ),
            )
        )
        turn_widget = QWidget()
        turn_layout = QVBoxLayout(turn_widget)
        turn_layout.addWidget(self._latency_chart, stretch=1)
        turn_layout.addWidget(table_view(self._latency_model), stretch=1)
        turn_layout.addWidget(QLabel("Slow Turns"))
        turn_layout.addWidget(table_view(self._slow_model), stretch=1)
        tabs = QTabWidget()
        tabs.addTab(ai_widget, "GenAI")
        tabs.addTab(turn_widget, "Turn latency")
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "AI 与 Turn",
                "把 provider/model 成本与 durable workflow 的端到端延迟放在同一业务视角中。",
            )
        )
        layout.addWidget(tabs, stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 同窗请求 GenAI 与 Turn 数据 / Request GenAI and Turn data for the same window."""

        return GenAiQuery(window, limit=150), LatencyQuery(window, slow_turn_limit=150)

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新对应业务标签页 / Update the corresponding business tab."""

        if isinstance(query, GenAiQuery):
            rows = cast(tuple[GenAiStats, ...], value)
            self._ai_model.replace(rows)
            self._ai_chart.plot_usage(rows)
        elif isinstance(query, LatencyQuery) and isinstance(value, LatencySnapshot):
            self._latency_model.replace(value.summary)
            self._slow_model.replace(value.slow_turns)
            self._latency_chart.plot_latency(value.summary)


class ResourcesPage(QueryPage):
    """@brief Service resource 生命周期页面 / Service-resource lifecycle page."""

    def __init__(self) -> None:
        """@brief 组装实例表 / Compose the instance table."""

        super().__init__()
        self._model = ObjectTableModel[ResourceInstance](
            (
                TableColumn("Active", lambda row: row.active),
                TableColumn("Service", lambda row: row.service_name),
                TableColumn("Version", lambda row: row.service_version),
                TableColumn("Environment", lambda row: row.environment),
                TableColumn("Instance", lambda row: row.instance_id, stretch=True),
                TableColumn("Started", lambda row: row.started_at),
                TableColumn("Stopped", lambda row: row.stopped_at),
            )
        )
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Resources",
                "检查服务版本、部署环境和实例生命周期，识别重启、漂移与残留实例。",
            )
        )
        layout.addWidget(table_view(self._model), stretch=1)

    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 返回资源查询 / Return the resource query."""

        return (ResourcesQuery(window, limit=500),)

    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 更新资源实例表 / Update the resource-instance table."""

        if isinstance(query, ResourcesQuery):
            self._model.replace(cast(tuple[ResourceInstance, ...], value))


__all__ = [
    "AiTurnsPage",
    "EventsPage",
    "MetricsPage",
    "OperationsPage",
    "OverviewPage",
    "ResourcesPage",
    "TracesPage",
]
