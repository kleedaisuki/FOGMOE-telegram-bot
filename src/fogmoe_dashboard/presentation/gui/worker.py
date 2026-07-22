"""@brief Qt 主线程之外的异步 Dashboard 查询执行器 / Async Dashboard query executor outside the Qt GUI thread."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from threading import Lock

from PyQt6.QtCore import QThread, pyqtSignal

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.application.queries import (
    DashboardQuery,
    DashboardResult,
    execute_query,
)

DashboardFactory = Callable[[], Dashboard]
"""@brief 惰性 Dashboard 工厂 / Lazy Dashboard factory."""


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """@brief 带世代号的后台查询请求 / Background query request carrying a generation."""

    request_id: int
    generation: int
    query: DashboardQuery


@dataclass(frozen=True, slots=True)
class QuerySuccess:
    """@brief 成功查询结果 envelope / Successful-query result envelope."""

    request: QueryRequest
    value: DashboardResult


@dataclass(frozen=True, slots=True)
class QueryFailure:
    """@brief 不泄漏连接信息的查询失败 / Query failure without connection-detail leakage."""

    request: QueryRequest
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class _Stop:
    """@brief 后台循环停止哨兵 / Background-loop stop sentinel."""


class DashboardWorker(QThread):
    """@brief 在单线程单 event loop 中拥有 asyncpg 生命周期 / Own asyncpg lifecycle in one thread and one event loop."""

    result_ready = pyqtSignal(object)
    """@brief 成功结果信号 / Successful-result signal."""
    query_failed = pyqtSignal(object)
    """@brief 查询失败信号 / Query-failure signal."""
    fatal_error = pyqtSignal(str)
    """@brief worker 初始化失败信号 / Worker-initialization failure signal."""

    def __init__(self, factory: DashboardFactory) -> None:
        """@brief 保存惰性工厂，避免在 GUI 线程创建连接 / Store a lazy factory to avoid GUI-thread connections."""

        super().__init__()
        self._factory = factory
        self._requests: Queue[QueryRequest | _Stop] = Queue()
        self._generation_lock = Lock()
        self._latest_generation = 0
        self.setObjectName("fogmoe-dashboard-query-worker")

    def submit(self, request: QueryRequest) -> None:
        """@brief 无阻塞提交查询 / Submit a query without blocking.

        @param request 强类型请求 / Strongly typed request.
        @return None / None.
        """

        with self._generation_lock:
            self._latest_generation = max(
                self._latest_generation,
                request.generation,
            )
        self._requests.put_nowait(request)

    def stop(self) -> None:
        """@brief 请求在当前查询后有序关闭 / Request orderly shutdown after the current query."""

        self._requests.put_nowait(_Stop())

    def run(self) -> None:
        """@brief 启动 worker 的 asyncio event loop / Start the worker asyncio event loop."""

        try:
            asyncio.run(self._serve())
        except BaseException as error:
            self.fatal_error.emit(f"{type(error).__name__}: {error}")

    async def _serve(self) -> None:
        """@brief 串行消费请求并复用同一连接池 / Serially consume requests while reusing one connection pool."""

        dashboard = self._factory()
        try:
            while True:
                request = await asyncio.to_thread(self._requests.get)
                if isinstance(request, _Stop):
                    return
                if self._is_stale(request):
                    continue
                try:
                    value = await execute_query(dashboard, request.query)
                except Exception as error:
                    self.query_failed.emit(
                        QueryFailure(
                            request=request,
                            error_type=type(error).__name__,
                            message=str(error),
                        )
                    )
                else:
                    self.result_ready.emit(QuerySuccess(request=request, value=value))
        finally:
            await dashboard.close()

    def _is_stale(self, request: QueryRequest) -> bool:
        """@brief 跳过尚未开始的旧刷新世代 / Skip an old refresh generation that has not started."""

        with self._generation_lock:
            return request.generation < self._latest_generation


__all__ = [
    "DashboardFactory",
    "DashboardWorker",
    "QueryFailure",
    "QueryRequest",
    "QuerySuccess",
]
