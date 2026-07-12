import asyncio
from contextlib import asynccontextmanager
from collections.abc import Iterable, Mapping
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fogmoe_bot.infrastructure import config

_ENGINE: AsyncEngine | None = None
_ENGINE_OWNER_LOOP: asyncio.AbstractEventLoop | None = None


def _quote_identifier(identifier: str) -> str:
    """@brief 引用 PostgreSQL 标识符 / Quote a PostgreSQL identifier.

    @param identifier 标识符 / Identifier.
    @return 双引号引用后的标识符 / Double-quoted identifier.
    """

    return '"' + identifier.replace('"', '""') + '"'


def _search_path() -> str:
    """@brief 构造 PostgreSQL search_path / Build PostgreSQL search_path.

    @return 逗号分隔的 schema 搜索路径 / Comma-separated schema search path.
    """

    schemas = [
        item.strip() for item in config.DB_SEARCH_PATH.split(",") if item.strip()
    ]
    return ", ".join(_quote_identifier(schema) for schema in schemas)


def _connect_args() -> dict[str, Any]:
    """@brief 构造 PostgreSQL 连接参数 / Build PostgreSQL connection args.

    @return SQLAlchemy connect_args / SQLAlchemy connect_args.
    """

    return {
        "timeout": config.DB_CONNECT_TIMEOUT,
        "server_settings": {
            "search_path": _search_path(),
        },
    }


def get_engine() -> AsyncEngine:
    """@brief 返回主 event loop 所有的唯一引擎 / Return the sole engine owned by the main event loop.

    @return 进程唯一 SQLAlchemy 异步引擎 / Process-wide SQLAlchemy async engine.
    @raise RuntimeError 引擎被另一个 event loop 使用 / The engine belongs to another event loop.
    @note 顶层组合根在所有数据库 worker 停止后调用 ``dispose_current_engine``；不再为已删除的
    secondary loops 保留 engine registry。/ The composition root calls ``dispose_current_engine``
    after every database worker stops; no engine registry remains for removed secondary loops.
    """

    global _ENGINE, _ENGINE_OWNER_LOOP

    loop = asyncio.get_running_loop()
    if _ENGINE is not None:
        if _ENGINE_OWNER_LOOP is not loop:
            raise RuntimeError("Database engine belongs to another event loop")
        return _ENGINE

    _ENGINE = create_async_engine(
        config.SQLALCHEMY_DATABASE_URI,
        pool_pre_ping=True,
        pool_recycle=config.DB_POOL_RECYCLE,
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        connect_args=_connect_args(),
    )
    _ENGINE_OWNER_LOOP = loop
    return _ENGINE


async def dispose_current_engine() -> None:
    """@brief 释放主 event loop 的数据库连接池 / Dispose the main event loop's database pool.

    @return None / None.
    @raise RuntimeError 从非 owner event loop 调用 / Called from a non-owner event loop.
    """

    global _ENGINE, _ENGINE_OWNER_LOOP

    engine = _ENGINE
    owner_loop = _ENGINE_OWNER_LOOP
    if engine is None:
        return
    loop = asyncio.get_running_loop()
    if owner_loop is not loop:
        raise RuntimeError("Database engine must be disposed by its owner event loop")
    _ENGINE = None
    _ENGINE_OWNER_LOOP = None
    await engine.dispose()


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncConnection]:
    """@brief 打开当前事件循环的数据库连接 / Open a connection for the current event loop.

    @return 异步连接上下文 / Async connection context.
    """

    engine = get_engine()
    async with engine.connect() as connection:
        yield connection


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    """@brief 打开自动提交或回滚的事务 / Open an auto-committing or rolling-back transaction.

    @return 异步事务连接上下文 / Async transactional connection context.
    """

    engine = get_engine()
    async with engine.begin() as connection:
        yield connection


async def exec_sql(
    sql: str,
    params: Iterable[Any] | Mapping[str, Any] | None = None,
    *,
    connection: AsyncConnection | None = None,
) -> CursorResult[Any]:
    """@brief 执行参数化 SQL / Execute parameterized SQL.

    @param sql SQL 文本 / SQL text.
    @param params 位置参数或命名参数 / Positional or named parameters.
    @param connection 可选的现有连接 / Optional existing connection.
    @return SQLAlchemy 游标结果 / SQLAlchemy cursor result.
    @note 未提供连接时仅适合读取；写入请经 transaction 或 connection.execute / Without a connection this is intended for reads; writes should use a transaction or connection.execute.
    """

    statement, bind_params = _prepare_statement(sql, params)
    if connection is None:
        async with connect() as connection:
            return await connection.execute(statement, bind_params)
    return await connection.execute(statement, bind_params)


def _prepare_statement(
    sql: str,
    params: Iterable[Any] | Mapping[str, Any] | None,
) -> tuple[TextClause, Mapping[str, Any]]:
    """@brief 准备 SQLAlchemy 文本语句 / Prepare a SQLAlchemy text statement.

    @param sql SQL 文本 / SQL text.
    @param params 参数；可为映射或旧式位置参数 / Parameters, either mapping or legacy positional values.
    @return SQLAlchemy text 与绑定参数 / SQLAlchemy text and bound parameters.
    @note 位置参数的 `%s` 会在数据库边界转换为 named bind / Positional `%s` placeholders are converted to named binds at the database boundary.
    """

    if params is None:
        return text(sql), {}
    if isinstance(params, Mapping):
        return text(sql), params

    values = tuple(params)
    placeholder_count = sql.count("%s")
    if placeholder_count != len(values):
        raise ValueError(
            f"SQL placeholder count {placeholder_count} does not match parameter count {len(values)}"
        )

    parts = sql.split("%s")
    rendered = [parts[0]]
    bind_params: dict[str, Any] = {}
    for index, value in enumerate(values):
        name = f"p{index}"
        rendered.append(f":{name}")
        rendered.append(parts[index + 1])
        bind_params[name] = value
    return text("".join(rendered)), bind_params
