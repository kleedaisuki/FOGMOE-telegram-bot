"""@brief 领域对象到 Qt 表格的类型化适配 / Typed domain-object to Qt-table adaptation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PyQt6.QtGui import QColor


RowT = TypeVar("RowT")
"""@brief 表格行领域类型 / Domain type represented by a table row."""


@dataclass(frozen=True, slots=True)
class TableColumn(Generic[RowT]):
    """@brief 一个类型化只读列 / A typed read-only column.

    @param title 用户可见标题 / User-visible title.
    @param value 从行读取原值的函数 / Function reading a raw value from a row.
    @param display 可选显示格式化器 / Optional display formatter.
    @param alignment 单元格对齐方式 / Cell alignment.
    @param stretch 是否优先拉伸 / Whether the column should preferentially stretch.
    """

    title: str
    value: Callable[[RowT], object]
    display: Callable[[object], str] | None = None
    alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft
    stretch: bool = False


class ObjectTableModel(QAbstractTableModel, Generic[RowT]):
    """@brief 不复制领域语义的通用只读表模型 / Generic read-only table model without duplicating domain semantics."""

    def __init__(
        self,
        columns: Sequence[TableColumn[RowT]],
        *,
        parent: QObject | None = None,
    ) -> None:
        """@brief 创建空模型 / Create an empty model.

        @param columns 封闭列定义 / Closed column definitions.
        @param parent 可选 Qt parent / Optional Qt parent.
        @return None / None.
        """

        super().__init__(parent)
        self._columns = tuple(columns)
        self._rows: tuple[RowT, ...] = ()

    def replace(self, rows: Sequence[RowT]) -> None:
        """@brief 原子替换不可变行快照 / Atomically replace the immutable row snapshot.

        @param rows 新行 / New rows.
        @return None / None.
        """

        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def item(self, row: int) -> RowT | None:
        """@brief 按可见行返回领域对象 / Return the domain object for a visible row.

        @param row 行号 / Row number.
        @return 合法行或 None / Valid row or None.
        """

        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """@brief 返回顶层行数 / Return the top-level row count."""

        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """@brief 返回顶层列数 / Return the top-level column count."""

        return 0 if parent.isValid() else len(self._columns)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        """@brief 返回显示、对齐或原始领域值 / Return display, alignment, or raw-domain data."""

        if not index.isValid():
            return None
        row = self.item(index.row())
        if row is None or not 0 <= index.column() < len(self._columns):
            return None
        column = self._columns[index.column()]
        value = column.value(row)
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                column.display(value)
                if column.display is not None
                else format_value(value)
            )
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(column.alignment | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.ForegroundRole and _is_error_value(
            column.title, value
        ):
            return QColor("#f87171")
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        """@brief 返回水平列标题 / Return horizontal column headings."""

        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._columns)
        ):
            return self._columns[section].title
        return None


def format_value(value: object) -> str:
    """@brief 统一格式化领域值 / Uniformly format a domain value.

    @param value 领域值 / Domain value.
    @return 紧凑显示文本 / Compact display text.
    """

    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def percent(value: object) -> str:
    """@brief 格式化 0..1 比率 / Format a ratio in the range 0..1."""

    if value is None:
        return "—"
    if not isinstance(value, int | float):
        raise TypeError("percent formatter requires a number")
    return f"{value:.2%}"


def milliseconds(value: object) -> str:
    """@brief 格式化毫秒 / Format milliseconds."""

    if value is None:
        return "—"
    if not isinstance(value, int | float):
        raise TypeError("milliseconds formatter requires a number")
    return f"{value:,.2f} ms"


def integer(value: object) -> str:
    """@brief 格式化整数 / Format an integer."""

    if value is None:
        return "—"
    if not isinstance(value, int):
        raise TypeError("integer formatter requires an integer")
    return f"{value:,}"


def _is_error_value(title: str, value: object) -> bool:
    """@brief 判断错误列的非零值 / Decide whether an error-column value is non-zero."""

    return "错误" in title and isinstance(value, int | float) and value > 0


__all__ = [
    "ObjectTableModel",
    "TableColumn",
    "format_value",
    "integer",
    "milliseconds",
    "percent",
]
