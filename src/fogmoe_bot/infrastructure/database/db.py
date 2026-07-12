import asyncio
import re
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql.elements import TextClause

from fogmoe_bot.application.observability.telemetry import SpanScope, Telemetry
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.infrastructure import config

_ENGINE: AsyncEngine | None = None
_ENGINE_OWNER_LOOP: asyncio.AbstractEventLoop | None = None
_TELEMETRY: Telemetry | None = None
"""@brief 数据库 client span recorder / Database-client span recorder."""
_INSTRUMENTED_ENGINE_ID: int | None = None
"""@brief 已安装事件 listener 的 sync engine identity / Identity of the instrumented synchronous engine."""

_SQL_TARGET = re.compile(
    r"\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)",
    re.IGNORECASE,
)
"""@brief 仅提取普通 schema.table 的安全 SQL target / Safe extractor for ordinary schema.table SQL targets."""


def configure_observability(telemetry: Telemetry) -> None:
    """@brief 为唯一数据库引擎配置安全 client spans / Configure safe client spans for the sole database engine.

    @param telemetry 进程 typed telemetry / Process typed telemetry.
    @return None / None.
    @note 不记录 SQL statement、绑定参数或数据库凭据 / SQL statements, bind parameters,
        and database credentials are never recorded.
    """

    global _TELEMETRY
    _TELEMETRY = telemetry
    if _ENGINE is not None:
        _instrument_engine(_ENGINE)


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
    _instrument_engine(_ENGINE)
    _ENGINE_OWNER_LOOP = loop
    return _ENGINE


def _instrument_engine(engine: AsyncEngine) -> None:
    """@brief 在 SQLAlchemy driver 边界安装一次 span hooks / Install span hooks once at the SQLAlchemy driver boundary.

    @param engine 异步业务引擎 / Asynchronous business engine.
    @return None / None.
    """

    global _INSTRUMENTED_ENGINE_ID
    sync_engine = engine.sync_engine
    if _configured_telemetry() is None or _INSTRUMENTED_ENGINE_ID == id(sync_engine):
        return

    @event.listens_for(sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """@brief 在 driver 调用前启动无 SQL 文本 span / Start a statement-free span before the driver call."""

        del cursor, parameters, context
        telemetry = _configured_telemetry()
        if telemetry is None:
            return
        operation = _sql_operation(statement)
        target = _sql_target(statement)
        summary = f"{operation} {target}" if target is not None else operation
        scope = telemetry.span(
            summary,
            kind=SpanKind.CLIENT,
            attributes={
                "db.system.name": "postgresql",
                "db.operation.name": operation,
                "db.operation.batch": executemany,
                **({"db.collection.name": target} if target is not None else {}),
                "db.query.summary": summary,
            },
        )
        scope.__enter__()
        spans = connection.info.setdefault("fogmoe.observability.spans", [])
        spans.append(scope)

    @event.listens_for(sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """@brief 成功后结束 client span / Complete the client span after success."""

        del cursor, statement, parameters, context, executemany
        _finish_database_span(connection, None)

    @event.listens_for(sync_engine, "handle_error")
    def handle_error(exception_context: Any) -> None:
        """@brief driver 失败时记录 error span / Record an error span after driver failure."""

        connection = exception_context.connection
        if connection is not None:
            _finish_database_span(connection, exception_context.original_exception)

    _INSTRUMENTED_ENGINE_ID = id(sync_engine)


def _configured_telemetry() -> Telemetry | None:
    """@brief 返回可选 recorder / Return the optional configured recorder.

    @return 进程 typed telemetry 或 None / Process typed telemetry or None.
    """

    return _TELEMETRY


def _finish_database_span(connection: Any, error: BaseException | None) -> None:
    """@brief 从连接栈弹出并结束最近 span / Pop and finish the most recent span from a connection stack."""

    raw_spans = connection.info.get("fogmoe.observability.spans")
    if not isinstance(raw_spans, list) or not raw_spans:
        return
    scope = raw_spans.pop()
    if not isinstance(scope, SpanScope):
        return
    if error is None:
        scope.__exit__(None, None, None)
        return
    scope.__exit__(type(error), error, error.__traceback__)


def _sql_operation(statement: str) -> str:
    """@brief 仅提取低基数 SQL verb / Extract only a low-cardinality SQL verb.

    @param statement SQLAlchemy 发送的 SQL / SQL sent by SQLAlchemy.
    @return 大写 verb 或 UNKNOWN / Uppercase verb or UNKNOWN.
    """

    normalized = statement.lstrip()
    if not normalized:
        return "UNKNOWN"
    verb = normalized.split(None, 1)[0].upper()
    return verb if verb in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"} else "OTHER"


def _sql_target(statement: str) -> str | None:
    """@brief 提取无参数、低基数 SQL target / Extract a parameter-free low-cardinality SQL target.

    @param statement SQLAlchemy 发送的 SQL / SQL sent by SQLAlchemy.
    @return ``schema.table`` 或 ``table``，无法安全识别时为 None /
        ``schema.table`` or ``table``; None when it cannot be identified safely.
    @note 不解析引号标识符、CTE 或动态 SQL，宁可缺失 target，也绝不持久化 SQL 文本 /
        Quoted identifiers, CTEs, and dynamic SQL are deliberately not parsed: missing a target
        is preferable to persisting SQL text.
    """

    match = _SQL_TARGET.search(statement)
    return match.group(1)[:255] if match is not None else None


async def dispose_current_engine() -> None:
    """@brief 释放主 event loop 的数据库连接池 / Dispose the main event loop's database pool.

    @return None / None.
    @raise RuntimeError 从非 owner event loop 调用 / Called from a non-owner event loop.
    """

    global _ENGINE, _ENGINE_OWNER_LOOP, _INSTRUMENTED_ENGINE_ID

    engine = _ENGINE
    owner_loop = _ENGINE_OWNER_LOOP
    if engine is None:
        return
    loop = asyncio.get_running_loop()
    if owner_loop is not loop:
        raise RuntimeError("Database engine must be disposed by its owner event loop")
    _ENGINE = None
    _ENGINE_OWNER_LOOP = None
    _INSTRUMENTED_ENGINE_ID = None
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
