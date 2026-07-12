"""@brief Dashboard GUI 无头组件与线程边界测试 / Headless Dashboard GUI component and thread-boundary tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer, Qt
from PyQt6.QtWidgets import QApplication

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.application.queries import HealthSeriesQuery, OverviewQuery
from fogmoe_dashboard.domain.models import (
    HealthPoint,
    Overview,
    PipelineStage,
    TimeWindow,
)
from fogmoe_dashboard.presentation.gui.pages import OverviewPage
from fogmoe_dashboard.presentation.gui.table import ObjectTableModel, TableColumn
from fogmoe_dashboard.presentation.gui.window import DashboardWindow
from fogmoe_dashboard.presentation.gui.worker import (
    DashboardWorker,
    QueryRequest,
    QuerySuccess,
)


_APP: QApplication | None = None
"""@brief 保持 QApplication Python wrapper 存活 / Keep the QApplication Python wrapper alive."""


class GuiRepository:
    """@brief GUI worker 烟测所需只读 repository / Read-only repository needed by the GUI-worker smoke test."""

    def __init__(self) -> None:
        """@brief 初始化关闭状态 / Initialize close state."""

        self.closed = False
        self.overview_calls = 0

    async def overview(self, window: TimeWindow) -> Overview:
        """@brief 返回固定总览 / Return a fixed overview."""

        self.overview_calls += 1
        return _overview(window)

    async def health_series(self, window: TimeWindow, *, buckets: int):
        """@brief 返回固定健康趋势 / Return a fixed health trend."""

        del buckets
        return _health(window)

    async def close(self) -> None:
        """@brief 记录有序关闭 / Record orderly closure."""

        self.closed = True


def _application() -> QApplication:
    """@brief 获取测试 QApplication / Get the test QApplication."""

    global _APP
    application = QApplication.instance()
    if application is None:
        application = QApplication(["fogmoe-dashboard-test"])
    _APP = application
    return application


def _drain_events(milliseconds: int = 120) -> None:
    """@brief 有界处理 Qt queued signals / Process Qt queued signals for a bounded interval."""

    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def test_object_table_model_preserves_domain_rows_and_roles() -> None:
    """@brief Qt model 不把领域对象降级成字符串矩阵 / Qt model does not degrade domain objects into a string matrix."""

    _application()
    model = ObjectTableModel[PipelineStage](
        (
            TableColumn("阶段", lambda row: row.stage),
            TableColumn(
                "等待", lambda row: row.pending, alignment=Qt.AlignmentFlag.AlignRight
            ),
        )
    )
    row = PipelineStage("inbox", 3, 0, 0, 0, None, 0)
    model.replace((row,))

    assert model.rowCount() == 1
    assert model.item(0) is row
    assert model.data(model.index(0, 0)) == "inbox"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.UserRole) == 3


def test_overview_page_renders_kpis_and_time_series_without_display() -> None:
    """@brief 总览页面在 offscreen Qt 中渲染领域快照 / Overview page renders domain snapshots in offscreen Qt."""

    _application()
    window = TimeWindow.last(timedelta(hours=1))
    page = OverviewPage()

    page.accept(OverviewQuery(window), _overview(window))
    page.accept(HealthSeriesQuery(window), _health(window))

    assert page._cards["spans"].text == "10"
    assert page._cards["error_rate"].text == "10.00%"
    assert len(page._chart.figure.axes) == 5
    page.close()


def test_main_window_executes_queries_in_worker_and_closes_cleanly() -> None:
    """@brief 主窗口跨线程取得结果且有序关闭 pool / Main window obtains cross-thread results and closes the pool orderly."""

    application = _application()
    repository = GuiRepository()
    window = DashboardWindow(
        lambda: Dashboard(repository),  # type: ignore[arg-type]
        initial_window=timedelta(hours=1),
    )
    window.show()
    _drain_events(250)

    assert len(window.pages) == 7
    overview = window.pages[0][1]
    assert isinstance(overview, OverviewPage)
    assert overview._cards["spans"].text == "10"

    window.close()
    application.processEvents()
    assert repository.closed


def test_worker_coalesces_queued_refresh_generations() -> None:
    """@brief worker 跳过未开始的旧世代，避免刷新风暴 / Worker skips unstarted stale generations to avoid refresh storms."""

    _application()
    repository = GuiRepository()
    worker = DashboardWorker(
        lambda: Dashboard(repository)  # type: ignore[arg-type]
    )
    results: list[QuerySuccess] = []
    worker.result_ready.connect(
        lambda value: results.append(value) if isinstance(value, QuerySuccess) else None
    )
    window = TimeWindow.last(timedelta(hours=1))
    worker.submit(QueryRequest(1, 1, OverviewQuery(window)))
    worker.submit(QueryRequest(2, 2, OverviewQuery(window)))
    worker.stop()
    loop = QEventLoop()
    worker.finished.connect(loop.quit)

    worker.start()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()

    assert worker.isFinished()
    assert repository.overview_calls == 1
    assert [result.request.generation for result in results] == [2]
    assert repository.closed


def _overview(window: TimeWindow) -> Overview:
    """@brief 创建 GUI 固定总览 / Create a fixed GUI overview."""

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
        pipeline=(PipelineStage("inbox", 1, 2, 0, 0, None, 0),),
    )


def _health(window: TimeWindow) -> tuple[HealthPoint, ...]:
    """@brief 创建 GUI 固定健康趋势 / Create a fixed GUI health trend."""

    start = datetime.fromtimestamp(window.start.timestamp(), UTC)
    return (
        HealthPoint(start, 1.0, 0.0, 4.0, 0, 50, 10),
        HealthPoint(start + timedelta(minutes=1), 2.0, 0.1, 8.0, 1, 50, 10),
    )
