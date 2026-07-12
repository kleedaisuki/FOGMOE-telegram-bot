"""@brief Dashboard GUI 页面的最小共享构件 / Minimal shared building blocks for Dashboard GUI pages."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import TypeVar

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from fogmoe_dashboard.application.queries import DashboardQuery, DashboardResult
from fogmoe_dashboard.domain.models import TimeWindow
from fogmoe_dashboard.presentation.gui.table import ObjectTableModel


RowT = TypeVar("RowT")
"""@brief table_view 保留的行类型 / Row type preserved by table_view."""


class QueryPage(QWidget):
    """@brief 以强类型查询描述数据需求的页面 / Page describing its data needs with strongly typed queries."""

    refresh_requested = pyqtSignal()
    """@brief 过滤器改变后的刷新请求 / Refresh request after a filter change."""
    query_requested = pyqtSignal(object)
    """@brief 页面内 drill-down 查询请求 / In-page drill-down query request."""

    @abstractmethod
    def queries(self, window: TimeWindow) -> tuple[DashboardQuery, ...]:
        """@brief 返回页面当前所需查询 / Return queries currently required by the page."""

    @abstractmethod
    def accept(self, query: DashboardQuery, value: DashboardResult) -> None:
        """@brief 接受与查询对应的新快照 / Accept a new snapshot corresponding to a query."""


class KpiCard(QFrame):
    """@brief 一个可访问的关键指标卡片 / An accessible key-performance indicator card."""

    def __init__(self, title: str) -> None:
        """@brief 创建指标标题与值 / Create a metric title and value."""

        super().__init__()
        self.setObjectName("kpiCard")
        self._title = QLabel(title)
        self._title.setObjectName("kpiTitle")
        self._value = QLabel("—")
        self._value.setObjectName("kpiValue")
        self._value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addWidget(self._value)

    def set_value(self, value: str, *, alert: bool = False) -> None:
        """@brief 更新显示值与告警语义 / Update the displayed value and alert semantics.

        @param value 已格式化值 / Formatted value.
        @param alert 是否进入告警色 / Whether to use alert coloring.
        @return None / None.
        """

        self._value.setText(value)
        self._value.setProperty("alert", alert)
        style = self._value.style()
        if style is not None:
            style.unpolish(self._value)
            style.polish(self._value)

    @property
    def text(self) -> str:
        """@brief 返回当前指标文本 / Return the current metric text."""

        return self._value.text()


def page_header(title: str, description: str) -> QWidget:
    """@brief 构造页面标题区 / Build a page title region.

    @param title 页面标题 / Page title.
    @param description 一句话操作提示 / One-sentence operational hint.
    @return 标题 widget / Header widget.
    """

    widget = QWidget()
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    description_label = QLabel(description)
    description_label.setObjectName("pageDescription")
    description_label.setWordWrap(True)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(2)
    layout.addWidget(title_label)
    layout.addWidget(description_label)
    return widget


def table_view(model: ObjectTableModel[RowT]) -> QTableView:
    """@brief 构造一致的只读可排序表格 / Build a consistent read-only sortable table.

    @param model 领域对象表模型 / Domain-object table model.
    @return 配置完成的 QTableView / Configured QTableView.
    """

    view = QTableView()
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    view.setShowGrid(False)
    vertical_header = view.verticalHeader()
    horizontal_header = view.horizontalHeader()
    if vertical_header is not None:
        vertical_header.setVisible(False)
    if horizontal_header is not None:
        horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        horizontal_header.setStretchLastSection(True)
    view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return view


def add_widgets(layout: QVBoxLayout, widgets: Sequence[QWidget]) -> None:
    """@brief 按序加入页面 widget / Add page widgets in order."""

    for widget in widgets:
        layout.addWidget(widget)


__all__ = ["KpiCard", "QueryPage", "add_widgets", "page_header", "table_view"]
