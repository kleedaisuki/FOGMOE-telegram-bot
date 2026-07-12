"""@brief 可供脚本调用的 Dashboard 公共 API / Public Dashboard API for scripts."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Self

from fogmoe_dashboard.application.dashboard import Dashboard
from fogmoe_dashboard.config import (
    DEFAULT_CONFIG_DIR,
    load_project_env,
    service_database_url,
)
from fogmoe_dashboard.infrastructure.postgres import PostgresDashboardRepository


class DashboardClient(Dashboard):
    """@brief 拥有 PostgreSQL 生命周期的脚本 API / Script API owning the PostgreSQL lifecycle.

    @example
        ``async with DashboardClient.from_environment() as dashboard:``
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
    ) -> DashboardClient:
        """@brief 从显式 URL 创建 client / Create a client from an explicit URL.

        @param database_url PostgreSQL 或 SQLAlchemy asyncpg URL / PostgreSQL or SQLAlchemy asyncpg URL.
        @param pool_size 最大连接数 / Maximum connections.
        @param command_timeout 单条命令超时 / Per-command timeout.
        @return 尚未连接的 client / Client not yet connected.
        """

        return cls(
            PostgresDashboardRepository(
                database_url,
                pool_size=pool_size,
                command_timeout=command_timeout,
            )
        )

    @classmethod
    def from_service(
        cls,
        *,
        config_dir: Path = DEFAULT_CONFIG_DIR,
        service_name: str = "fogmoe_automation",
        pool_size: int = 4,
        command_timeout: float = 5.0,
    ) -> DashboardClient:
        """@brief 从项目 libpq service 创建 client / Create a client from a project libpq service.

        @param config_dir pg_service.conf 与 pgpass 目录 / Directory containing pg_service.conf and pgpass.
        @param service_name service section 名 / Service section name.
        @param pool_size 最大连接数 / Maximum connections.
        @param command_timeout 单条命令超时 / Per-command timeout.
        @return 尚未连接的 client / Client not yet connected.
        """

        return cls.from_database_url(
            service_database_url(config_dir.resolve(), service_name),
            pool_size=pool_size,
            command_timeout=command_timeout,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        config_dir: Path = DEFAULT_CONFIG_DIR,
        service_name: str = "fogmoe_automation",
        pool_size: int = 4,
        command_timeout: float = 5.0,
    ) -> DashboardClient:
        """@brief 优先从 DATABASE_URL，否则从 service 创建 / Create from DATABASE_URL first, then from a service.

        @param config_dir service 配置目录 / Service configuration directory.
        @param service_name fallback service 名 / Fallback service name.
        @param pool_size 最大连接数 / Maximum connections.
        @param command_timeout 单条命令超时 / Per-command timeout.
        @return 尚未连接的 client / Client not yet connected.
        """

        load_project_env()
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            return cls.from_database_url(
                database_url,
                pool_size=pool_size,
                command_timeout=command_timeout,
            )
        return cls.from_service(
            config_dir=config_dir,
            service_name=service_name,
            pool_size=pool_size,
            command_timeout=command_timeout,
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
