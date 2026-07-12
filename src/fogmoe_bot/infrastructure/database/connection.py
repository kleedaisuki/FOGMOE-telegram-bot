"""Typed asynchronous database primitives shared by infrastructure adapters."""

from collections.abc import Iterable, Mapping
from typing import Any, Literal, overload

from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import db

# SQL 参数类型 / SQL parameter type.
SqlParams = Iterable[Any] | Mapping[str, Any] | None

# 保留简洁的事务边界别名 / Keep concise transaction-boundary aliases.
connect = db.connect
transaction = db.transaction


@overload
async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[False] = False,
    connection: AsyncConnection | None = None,
) -> Row[Any] | None: ...


@overload
async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[True],
    connection: AsyncConnection | None = None,
) -> RowMapping | None: ...


async def fetch_one(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: bool = False,
    connection: AsyncConnection | None = None,
) -> Row[Any] | RowMapping | None:
    """@brief 读取至多一行 / Fetch at most one row.

    @param sql 参数化 SQL 文本 / Parameterized SQL text.
    @param params 位置参数或命名参数 / Positional or named parameters.
    @param mapping 是否返回列名映射 / Whether to return a column-name mapping.
    @param connection 可选的现有事务连接 / Optional existing transactional connection.
    @return 首行，不存在时为 None / First row, or None when absent.
    """

    result: CursorResult[Any] = await db.exec_sql(
        sql,
        params,
        connection=connection,
    )
    if mapping:
        return result.mappings().first()
    return result.fetchone()


@overload
async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[False] = False,
    connection: AsyncConnection | None = None,
) -> list[Row[Any]]: ...


@overload
async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: Literal[True],
    connection: AsyncConnection | None = None,
) -> list[RowMapping]: ...


async def fetch_all(
    sql: str,
    params: SqlParams = None,
    *,
    mapping: bool = False,
    connection: AsyncConnection | None = None,
) -> list[Row[Any]] | list[RowMapping]:
    """@brief 读取全部结果行 / Fetch all result rows.

    @param sql 参数化 SQL 文本 / Parameterized SQL text.
    @param params 位置参数或命名参数 / Positional or named parameters.
    @param mapping 是否返回列名映射 / Whether to return column-name mappings.
    @param connection 可选的现有事务连接 / Optional existing transactional connection.
    @return 物化后的结果列表 / Materialized result list.
    """

    result: CursorResult[Any] = await db.exec_sql(
        sql,
        params,
        connection=connection,
    )
    if mapping:
        return list(result.mappings().all())
    return list(result.fetchall())


async def execute(
    sql: str,
    params: SqlParams = None,
    *,
    connection: AsyncConnection | None = None,
) -> int:
    """@brief 执行写语句并返回影响行数 / Execute a write and return its row count.

    @param sql 参数化 SQL 文本 / Parameterized SQL text.
    @param params 位置参数或命名参数 / Positional or named parameters.
    @param connection 可选的现有事务连接 / Optional existing transactional connection.
    @return 数据库报告的影响行数 / Database-reported affected row count.
    @note 未提供连接时自动创建并提交独立事务 / Creates and commits an independent transaction when no connection is supplied.
    """

    if connection is None:
        async with transaction() as transaction_connection:
            result = await db.exec_sql(
                sql,
                params,
                connection=transaction_connection,
            )
            return result.rowcount

    result = await db.exec_sql(sql, params, connection=connection)
    return result.rowcount
