"""@brief Dashboard GUI 的 composition-independent 主窗口 / Composition-independent main window for the Dashboard GUI."""

from __future__ import annotations

from datetime import timedelta

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from fogmoe_dashboard.application.queries import DashboardQuery, TraceQuery
from fogmoe_dashboard.domain.models import TimeWindow
from fogmoe_dashboard.presentation.gui.base import QueryPage
from fogmoe_dashboard.presentation.gui.pages import (
    AiTurnsPage,
    EventsPage,
    MetricsPage,
    OperationsPage,
    OverviewPage,
    ReliabilityPage,
    ResourcesPage,
    RetrievalPage,
    TracesPage,
)
from fogmoe_dashboard.presentation.gui.worker import (
    DashboardFactory,
    DashboardWorker,
    QueryFailure,
    QueryRequest,
    QuerySuccess,
)


_WINDOWS = (
    ("15 分钟", 15 * 60),
    ("1 小时", 60 * 60),
    ("6 小时", 6 * 60 * 60),
    ("24 小时", 24 * 60 * 60),
    ("7 天", 7 * 24 * 60 * 60),
    ("30 天", 30 * 24 * 60 * 60),
)
"""@brief 可审计的预设分析窗口 / Auditable preset analytics windows."""


class DashboardWindow(QMainWindow):
    """@brief 协调导航、刷新世代与后台查询的主窗口 / Main window coordinating navigation, refresh generations, and background queries."""

    def __init__(
        self,
        factory: DashboardFactory,
        *,
        initial_window: timedelta = timedelta(hours=1),
        auto_refresh_seconds: int = 0,
    ) -> None:
        """@brief 组装页面并启动后台 worker / Compose pages and start the background worker.

        @param factory 在 worker 线程执行的 Dashboard 工厂 / Dashboard factory executed in the worker thread.
        @param initial_window 初始回看窗口 / Initial lookback window.
        @param auto_refresh_seconds 自动刷新间隔；零表示关闭 / Auto-refresh interval; zero disables it.
        @return None / None.
        """

        super().__init__()
        self.setWindowTitle("FogMoe Observability Dashboard")
        self.resize(1440, 900)
        self.setMinimumSize(1000, 680)
        self._generation = 0
        self._request_id = 0
        self._pending: set[int] = set()
        self._closing = False
        self._pages: tuple[tuple[str, QueryPage], ...] = (
            ("总览", OverviewPage()),
            ("操作", OperationsPage()),
            ("事件与日志", EventsPage()),
            ("Traces", TracesPage()),
            ("Metrics", MetricsPage()),
            ("可靠性", ReliabilityPage()),
            ("Retrieval", RetrievalPage()),
            ("AI 与 Turn", AiTurnsPage()),
            ("Resources", ResourcesPage()),
        )
        self._navigation = QListWidget()
        self._navigation.setObjectName("navigation")
        self._navigation.setFixedWidth(170)
        self._stack = QStackedWidget()
        for label, page in self._pages:
            self._navigation.addItem(QListWidgetItem(label))
            self._stack.addWidget(page)
            page.refresh_requested.connect(self.refresh)
            page.query_requested.connect(self._drill_down)
        self._navigation.currentRowChanged.connect(self._navigate)
        self._window = QComboBox()
        for label, seconds in _WINDOWS:
            self._window.addItem(label, seconds)
        self._select_window(initial_window)
        self._window.currentIndexChanged.connect(self.refresh)
        self._refresh_button = QPushButton("刷新")
        self._refresh_button.setObjectName("refreshButton")
        self._refresh_button.clicked.connect(self.refresh)
        self._auto_refresh = QCheckBox("自动刷新")
        self._interval = QSpinBox()
        self._interval.setRange(2, 300)
        self._interval.setSuffix(" s")
        self._interval.setValue(max(5, auto_refresh_seconds or 10))
        self._auto_refresh.toggled.connect(self._configure_timer)
        self._interval.valueChanged.connect(self._configure_timer)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("窗口"))
        toolbar.addWidget(self._window)
        toolbar.addStretch(1)
        toolbar.addWidget(self._auto_refresh)
        toolbar.addWidget(self._interval)
        toolbar.addWidget(self._refresh_button)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 12, 18, 12)
        content_layout.addLayout(toolbar)
        content_layout.addWidget(self._stack, stretch=1)
        central = QWidget()
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._navigation)
        central_layout.addWidget(content, stretch=1)
        self.setCentralWidget(central)
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._worker = DashboardWorker(factory)
        self._worker.result_ready.connect(self._accept_success)
        self._worker.query_failed.connect(self._accept_failure)
        self._worker.fatal_error.connect(self._fatal_error)
        self._worker.start()
        self.setStyleSheet(_STYLE)
        self._navigation.setCurrentRow(0)
        if auto_refresh_seconds > 0:
            self._interval.setValue(auto_refresh_seconds)
            self._auto_refresh.setChecked(True)

    @property
    def pages(self) -> tuple[tuple[str, QueryPage], ...]:
        """@brief 返回只读页面注册表 / Return the read-only page registry."""

        return self._pages

    def refresh(self) -> None:
        """@brief 创建新刷新世代并提交当前页面查询 / Create a refresh generation and submit current-page queries."""

        if self._closing:
            return
        self._generation += 1
        self._pending.clear()
        window = TimeWindow.last(timedelta(seconds=int(self._window.currentData())))
        page = self._current_page()
        for query in page.queries(window):
            self._submit(query)
        self._show_busy()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        """@brief 有序关闭 worker 与数据库连接池 / Orderly close the worker and database pool."""

        if a0 is None:
            return
        self._closing = True
        self._timer.stop()
        self._worker.stop()
        if self._worker.wait(7000):
            a0.accept()
            return
        self._status.showMessage("正在等待数据库查询结束…")
        self._closing = False
        a0.ignore()

    def _navigate(self, index: int) -> None:
        """@brief 切换页面并立即刷新 / Switch pages and refresh immediately."""

        if 0 <= index < len(self._pages):
            self._stack.setCurrentIndex(index)
            self.refresh()

    def _drill_down(self, query: object) -> None:
        """@brief 接收页面下钻并保持世代一致性 / Accept a page drill-down while preserving generation consistency."""

        if not isinstance(query, TraceQuery):
            return
        trace_index = next(
            index for index, (label, _) in enumerate(self._pages) if label == "Traces"
        )
        if self._navigation.currentRow() != trace_index:
            self._navigation.setCurrentRow(trace_index)
        self._submit(query)
        self._show_busy()

    def _submit(self, query: DashboardQuery) -> None:
        """@brief 为查询分配单调 request id / Assign a monotonic request id to a query."""

        self._request_id += 1
        self._pending.add(self._request_id)
        self._worker.submit(
            QueryRequest(
                request_id=self._request_id,
                generation=self._generation,
                query=query,
            )
        )

    def _accept_success(self, payload: object) -> None:
        """@brief 丢弃旧世代结果并更新当前页面 / Discard stale generations and update the current page."""

        if not isinstance(payload, QuerySuccess):
            return
        request = payload.request
        if request.generation != self._generation:
            return
        self._pending.discard(request.request_id)
        if isinstance(request.query, TraceQuery):
            trace_page = next(page for label, page in self._pages if label == "Traces")
            trace_page.accept(request.query, payload.value)
        else:
            self._current_page().accept(request.query, payload.value)
        self._show_ready()

    def _accept_failure(self, payload: object) -> None:
        """@brief 呈现当前世代查询错误 / Present a current-generation query error."""

        if not isinstance(payload, QueryFailure):
            return
        if payload.request.generation != self._generation:
            return
        self._pending.discard(payload.request.request_id)
        self._status.showMessage(
            f"查询失败 · {payload.error_type}: {payload.message}",
            15000,
        )
        self._refresh_button.setEnabled(True)

    def _fatal_error(self, message: str) -> None:
        """@brief 呈现 worker 致命错误 / Present a fatal worker error."""

        self._pending.clear()
        self._status.showMessage(f"Dashboard worker 已停止 · {message}")
        self._refresh_button.setEnabled(False)

    def _current_page(self) -> QueryPage:
        """@brief 返回当前强类型页面 / Return the current strongly typed page."""

        index = max(0, self._stack.currentIndex())
        return self._pages[index][1]

    def _configure_timer(self) -> None:
        """@brief 应用自动刷新开关与间隔 / Apply auto-refresh toggle and interval."""

        if self._auto_refresh.isChecked():
            self._timer.start(self._interval.value() * 1000)
        else:
            self._timer.stop()

    def _select_window(self, duration: timedelta) -> None:
        """@brief 选择最接近的预设窗口 / Select the nearest preset window."""

        seconds = duration.total_seconds()
        best = min(
            range(len(_WINDOWS)),
            key=lambda index: abs(_WINDOWS[index][1] - seconds),
        )
        self._window.setCurrentIndex(best)

    def _show_busy(self) -> None:
        """@brief 呈现非阻塞加载状态 / Present non-blocking loading state."""

        self._refresh_button.setEnabled(False)
        self._refresh_button.setText("查询中…")
        self._status.showMessage(
            f"正在查询 · {len(self._pending)} request(s) · generation {self._generation}"
        )

    def _show_ready(self) -> None:
        """@brief 在当前世代完成时恢复就绪状态 / Restore ready state when the current generation completes."""

        if self._pending:
            self._status.showMessage(f"正在查询 · 剩余 {len(self._pending)} request(s)")
            return
        self._refresh_button.setEnabled(True)
        self._refresh_button.setText("刷新")
        self._status.showMessage("已更新", 3000)


_STYLE = """
QMainWindow, QWidget { background: #111827; color: #e5e7eb; }
QListWidget#navigation { background: #0b1220; border: 0; padding: 12px 8px; }
QListWidget#navigation::item { padding: 11px 12px; margin: 2px; border-radius: 6px; }
QListWidget#navigation::item:selected { background: #164e63; color: #ecfeff; }
QLabel#pageTitle { font-size: 22px; font-weight: 650; color: #f9fafb; }
QLabel#pageDescription { color: #9ca3af; }
QFrame#kpiCard { background: #1f2937; border: 1px solid #374151; border-radius: 8px; }
QLabel#kpiTitle { color: #9ca3af; font-size: 11px; }
QLabel#kpiValue { color: #f9fafb; font-size: 21px; font-weight: 650; }
QLabel#kpiValue[alert="true"] { color: #fb7185; }
QTableView { background: #111827; alternate-background-color: #172033; border: 1px solid #374151; selection-background-color: #164e63; }
QHeaderView::section { background: #1f2937; color: #d1d5db; border: 0; border-right: 1px solid #374151; padding: 7px; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: #1f2937; border: 1px solid #4b5563; border-radius: 5px; padding: 6px; }
QPushButton { background: #155e75; border: 0; border-radius: 5px; padding: 7px 14px; color: #ecfeff; }
QPushButton:hover { background: #0e7490; }
QPushButton:disabled { background: #374151; color: #9ca3af; }
QTabWidget::pane { border: 1px solid #374151; }
QTabBar::tab { background: #1f2937; padding: 7px 14px; }
QTabBar::tab:selected { background: #164e63; }
QStatusBar { background: #0b1220; color: #9ca3af; }
"""
"""@brief Dashboard 深色高对比主题 / Dashboard dark high-contrast theme."""


__all__ = ["DashboardWindow"]
