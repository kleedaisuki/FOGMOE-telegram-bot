import asyncio
import contextvars
import threading
import weakref
from contextlib import asynccontextmanager
from collections.abc import Iterable, Mapping
from typing import Any, AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fogmoe_bot.infrastructure import config

_ENGINES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine] = (
    weakref.WeakKeyDictionary()
)
_ENGINE_LOCK = threading.Lock()
_DEFAULT_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
_BOUND_LOOP: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = (
    contextvars.ContextVar("database_bound_loop", default=None)
)


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
    """@brief 返回当前 event loop 专属引擎 / Return the engine owned by the current event loop.

    @return 当前 loop 的 SQLAlchemy 异步引擎 / Async SQLAlchemy engine for the current loop.
    @note SQLAlchemy pooled async engine 不能跨 event loop 共享；调度守护线程必须使用自己的连接池 /
    A pooled SQLAlchemy async engine cannot be shared across event loops, so the scheduling daemon owns a separate pool.
    """

    loop = asyncio.get_running_loop()
    with _ENGINE_LOCK:
        engine = _ENGINES.get(loop)
        if engine is None:
            engine = create_async_engine(
                config.SQLALCHEMY_DATABASE_URI,
                pool_pre_ping=True,
                pool_recycle=config.DB_POOL_RECYCLE,
                pool_size=config.DB_POOL_SIZE,
                max_overflow=config.DB_MAX_OVERFLOW,
                connect_args=_connect_args(),
            )
            _ENGINES[loop] = engine
        return engine


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """@brief 设置默认应用 event loop / Set the default application event loop.

    @param loop Telegram 应用 event loop / Telegram application event loop.
    @return None / None.
    """

    global _DEFAULT_MAIN_LOOP
    _DEFAULT_MAIN_LOOP = loop
    bind_loop(loop)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """@brief 将当前执行上下文绑定到一个 event loop / Bind the current execution context to an event loop.

    @param loop 当前上下文应回投的 event loop / Event loop used by the current execution context.
    @return None / None.
    @note 用于从线程池同步代码回投数据库协程；调用方需传播 contextvars /
    Used to route database coroutines from thread-pool code; callers must propagate contextvars.
    """

    _BOUND_LOOP.set(loop)


async def dispose_current_engine() -> None:
    """@brief 释放当前 event loop 的数据库连接池 / Dispose the current event loop's database pool.

    @return None / None.
    """

    loop = asyncio.get_running_loop()
    with _ENGINE_LOCK:
        engine = _ENGINES.pop(loop, None)
    if engine is not None:
        await engine.dispose()


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
    """@brief 从同步代码执行数据库协程 / Run a database coroutine from synchronous code.

    @param coro 要执行的协程 / Coroutine to execute.
    @return 协程结果 / Coroutine result.
    @note 优先使用 context-bound loop，未绑定时回退 Telegram 主 loop /
    Prefers the context-bound loop and falls back to the Telegram main loop.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = _BOUND_LOOP.get() or _DEFAULT_MAIN_LOOP
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
            return await connection.execute(statement, bind_params)
    return await connection.execute(statement, bind_params)


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
