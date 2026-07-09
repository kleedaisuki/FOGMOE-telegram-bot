import asyncio
from contextlib import asynccontextmanager
from collections.abc import Iterable, Mapping
from typing import Any, AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fogmoe_bot.infrastructure import config

_ENGINE: Optional[AsyncEngine] = None
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


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
        item.strip()
        for item in config.DB_SEARCH_PATH.split(",")
        if item.strip()
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
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_async_engine(
            config.SQLALCHEMY_DATABASE_URI,
            pool_pre_ping=True,
            pool_recycle=config.DB_POOL_RECYCLE,
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            connect_args=_connect_args(),
        )
    return _ENGINE


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


@asynccontextmanager
async def connect() -> AsyncIterator[AsyncConnection]:
    engine = get_engine()
    async with engine.connect() as connection:
        yield connection


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    engine = get_engine()
    async with engine.begin() as connection:
        yield connection


def run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _MAIN_LOOP
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()
        return asyncio.run(coro)
    raise RuntimeError("run_sync cannot be used inside a running event loop")


async def exec_sql(
    sql: str,
    params: Optional[Iterable[Any] | Mapping[str, Any]] = None,
    *,
    connection: Optional[AsyncConnection] = None,
):
    statement, bind_params = _prepare_statement(sql, params)
    if connection is None:
        async with connect() as connection:
            return await db_connection.execute(statement, bind_params)
    return await db_connection.execute(statement, bind_params)


def _prepare_statement(
    sql: str,
    params: Optional[Iterable[Any] | Mapping[str, Any]],
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
