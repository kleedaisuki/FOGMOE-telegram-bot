"""@brief dbctl PostgreSQL 显式连接原语 / dbctl PostgreSQL explicit-connection primitives."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.engine import URL


@dataclass(frozen=True)
class RoleSecret:
    """@brief 数据库角色凭据 / Database role credentials.

    @param role PostgreSQL 角色名 / PostgreSQL role name.
    @param password PostgreSQL 登录密码 / PostgreSQL login password.
    """

    role: str
    password: str


def quote_identifier(value: str) -> str:
    """@brief 引用 PostgreSQL 标识符 / Quote a PostgreSQL identifier.

    @param value 标识符原文 / Raw identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """@brief 引用 PostgreSQL 字面量 / Quote a PostgreSQL literal.

    @param value 字面量原文 / Raw literal.
    @return 单引号引用后的字面量 / Single-quoted literal.
    """

    return "'" + value.replace("'", "''") + "'"


def sqlalchemy_url(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> str:
    """@brief 构造正确转义的 SQLAlchemy URL / Build an escaped SQLAlchemy URL.

    @param host 数据库主机 / Database host.
    @param port 数据库端口 / Database port.
    @param database 数据库名 / Database name.
    @param user PostgreSQL 用户 / PostgreSQL user.
    @param password PostgreSQL 密码 / PostgreSQL password.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password or None,
        host=host,
        port=port,
        database=database,
    )
    return url.render_as_string(hide_password=False)


def direct_psql_environment(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str | None,
) -> dict[str, str]:
    """@brief 构造显式 PostgreSQL 连接环境 / Build an explicit PostgreSQL connection environment.

    @param host PostgreSQL 主机 / PostgreSQL host.
    @param port PostgreSQL 端口 / PostgreSQL port.
    @param database PostgreSQL 数据库名 / PostgreSQL database name.
    @param user PostgreSQL 登录角色 / PostgreSQL login role.
    @param password PostgreSQL 密码；``None`` 表示不提供密码 / PostgreSQL password; ``None`` omits it.
    @return 供一个 ``psql`` 子进程使用的环境 / Environment for one ``psql`` subprocess.
    @note 丢弃所有继承的 ``PG*`` 变量，避免外部 shell 的连接配置覆盖显式设置。/
        All inherited ``PG*`` variables are discarded so ambient shell connection settings cannot override explicit settings.
    """

    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PG")
    }
    environment.update(
        {
            "PGHOST": host,
            "PGPORT": str(port),
            "PGDATABASE": database,
            "PGUSER": user,
        }
    )
    if password is not None:
        environment["PGPASSWORD"] = password
    return environment
