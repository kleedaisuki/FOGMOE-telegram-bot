"""@brief 可供脚本调用的 Dashboard 公共 API / Public Dashboard API for scripts."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.config import DashboardSettings
from fogmoe_dashboard.domain.models import RESOURCE_STALE_AFTER
from fogmoe_dashboard.infrastructure.postgres import PostgresDashboardRepository


class DashboardClient(Dashboard):
    """@brief 拥有 PostgreSQL 生命周期的脚本 API / Script API owning the PostgreSQL lifecycle.

    @example
        ``async with DashboardClient.from_database_settings(settings=settings) as dashboard:``
        ``    overview = await dashboard.overview(TimeWindow.last(timedelta(hours=1)))``
        / Use the asynchronous context manager so connections are always closed.
    """

    @classmethod
    def from_database_url(
        cls,
        database_url: str,
        *,
        pool_size: int = 4,
        command_timeout: float = 5.0,
        resource_stale_after_seconds: float = RESOURCE_STALE_AFTER.total_seconds(),
    ) -> DashboardClient:
        """@brief 从显式 URL 创建 client / Create a client from an explicit URL.

        @param database_url PostgreSQL 或 SQLAlchemy asyncpg URL / PostgreSQL or SQLAlchemy asyncpg URL.
        @param pool_size 最大连接数 / Maximum connections.
        @param command_timeout 单条命令超时 / Per-command timeout.
        @param resource_stale_after_seconds 资源心跳失活阈值秒数 /
            Resource-heartbeat stale threshold in seconds.
        @return 尚未连接的 client / Client not yet connected.
        """

        return cls(
            PostgresDashboardRepository(
                database_url,
                pool_size=pool_size,
                command_timeout=command_timeout,
                resource_stale_after_seconds=resource_stale_after_seconds,
            )
        )

    @classmethod
    def from_database_settings(
        cls,
        *,
        settings: DashboardSettings,
    ) -> DashboardClient:
        """@brief 从类型化 Dashboard 设置创建 client / Create a client from typed Dashboard settings.

        @param settings Dashboard 拥有的数据库与查询设置 / Dashboard-owned database and query settings.
        @return 尚未连接的 client / Client not yet connected.
        """

        return cls.from_database_url(
            settings.database_url(),
            pool_size=settings.query.pool_size,
            command_timeout=settings.query.command_timeout_seconds,
            resource_stale_after_seconds=(settings.query.resource_stale_after_seconds),
        )

    async def __aenter__(self) -> Self:
        """@brief 进入异步 client 生命周期 / Enter the asynchronous client lifecycle.

        @return 当前 client / This client.
        """

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """@brief 无条件关闭连接池 / Unconditionally close the connection pool.

        @param exc_type 可选异常类型 / Optional exception type.
        @param exc 可选异常 / Optional exception.
        @param traceback 可选 traceback / Optional traceback.
        @return None / None.
        """

        del exc_type, exc, traceback
        await self.close()


__all__ = ["DashboardClient"]
