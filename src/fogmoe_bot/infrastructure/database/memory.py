"""@brief PostgreSQL 长期记忆只读投影 / PostgreSQL long-term-memory read projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

import regex

from fogmoe_bot.application.memory.queries import (
    MemoryPageQuery,
    MemorySearchQuery,
    MemorySearchResult,
)
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.memory.models import (
    MemoryId,
    MemoryProvenance,
    MemoryRecord,
    MemorySearchHit,
    MemorySourceKind,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


_MEMORY_COLUMNS = (
    "memory_id, source_kind, conversation_id, owner_user_id, source_digest, "
    "snapshot, legacy_record_id, summary_text, source_id, created_at"
)
"""@brief Memory read model 所需最小列 / Minimal columns required by the memory read model."""

_REGEX_TIMEOUT_SECONDS = 0.05
"""@brief 单记录正则执行上限 / Per-record regex execution limit."""


class PostgresMemoryReader:
    """@brief 从当前 durable artifact 构造独立 memory DTO / Build independent memory DTOs from current durable artifacts."""

    async def count_summaries(self, owner_user_id: int) -> int:
        """@brief 按 entitlement 统计可见摘要 / Count visible summaries under entitlement.

        @param owner_user_id 所有者 / Owning user.
        @return 可见摘要数 / Visible summary count.
        """

        _validate_owner(owner_user_id)
        row = await db_connection.fetch_one(
            "WITH ranked AS ("
            "SELECT memory_id, summary_text, ROW_NUMBER() OVER ("
            "ORDER BY created_at DESC, memory_id DESC) AS memory_rank, "
            "GREATEST(account.permanent_records_limit, 0) AS memory_limit "
            "FROM memory.records AS record "
            "JOIN identity.users AS account ON account.id = record.owner_user_id "
            "WHERE record.owner_user_id = %s"
            ") SELECT COUNT(*) FROM ranked WHERE memory_rank <= memory_limit "
            "AND summary_text IS NOT NULL AND summary_text <> ''",
            (owner_user_id,),
        )
        return _integer(row[0]) if row is not None else 0

    async def read_page(self, query: MemoryPageQuery) -> tuple[MemoryRecord, ...]:
        """@brief 读取 entitlement 内的有界 memory page / Read a bounded memory page under entitlement.

        @param query 已校验分页查询 / Validated page query.
        @return 不可变 memory records / Immutable memory records.
        """

        summary_filter = (
            "AND record.summary_text IS NOT NULL AND record.summary_text <> '' "
            if query.summaries_only
            else ""
        )
        direction = "DESC" if query.newest_first else "ASC"
        rows = await db_connection.fetch_all(
            "WITH ranked AS ("
            "SELECT record.memory_id, ROW_NUMBER() OVER ("
            "ORDER BY record.created_at DESC, record.memory_id DESC) "
            "AS memory_rank, "
            "GREATEST(account.permanent_records_limit, 0) AS memory_limit "
            "FROM memory.records AS record "
            "JOIN identity.users AS account ON account.id = record.owner_user_id "
            "WHERE record.owner_user_id = %s"
            ") SELECT "
            + ", ".join(
                f"record.{column.strip()}" for column in _MEMORY_COLUMNS.split(",")
            )
            + " FROM memory.records AS record "
            "JOIN ranked ON ranked.memory_id = record.memory_id "
            "WHERE ranked.memory_rank <= ranked.memory_limit "
            + summary_filter
            + f"ORDER BY record.created_at {direction}, "
            f"record.memory_id {direction} LIMIT %s OFFSET %s",
            (query.owner_user_id, query.limit, query.offset),
        )
        return tuple(_map_record(row) for row in rows)

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """@brief 用有超时的正则执行兼容检索 / Execute compatible regex search with a timeout.

        @param query 已校验检索查询 / Validated search query.
        @return 有界命中与非致命警告 / Bounded hits and non-fatal warning.
        """

        matcher, warning = _compile_pattern(query.pattern)
        records = await self.read_page(
            MemoryPageQuery(
                owner_user_id=query.owner_user_id,
                newest_first=not query.oldest_first,
                limit=min(500, max(50, query.limit * 20)),
            )
        )
        hits: list[MemorySearchHit] = []
        for record in records:
            text = json.dumps(record.snapshot, ensure_ascii=False, default=str)
            try:
                match = matcher.search(text, timeout=_REGEX_TIMEOUT_SECONDS)
            except TimeoutError:
                warning = "Regex evaluation timed out for one or more records"
                continue
            if match is None:
                continue
            hits.append(
                MemorySearchHit(
                    memory_id=record.memory_id,
                    created_at=record.created_at,
                    excerpt=text[max(0, match.start() - 300) : match.end() + 300],
                )
            )
            if len(hits) >= query.limit:
                break
        return MemorySearchResult(tuple(hits), warning)


def _compile_pattern(pattern: str) -> tuple[regex.Pattern[str], str | None]:
    """@brief 编译有界正则并对非法语法降级为 literal / Compile a bounded regex and fall back to a literal.

    @param pattern 已校验模式 / Validated pattern.
    @return matcher 与可选警告 / Matcher and optional warning.
    """

    try:
        return regex.compile(pattern, regex.IGNORECASE | regex.DOTALL), None
    except regex.error:
        return (
            regex.compile(regex.escape(pattern), regex.IGNORECASE | regex.DOTALL),
            "Invalid regex; matched as literal text",
        )


def _map_record(row: object) -> MemoryRecord:
    """@brief 将数据库 row 映射为 memory record / Map a database row to a memory record.

    @param row SQLAlchemy row / SQLAlchemy row.
    @return 已校验 memory record / Validated memory record.
    """

    values = _row_values(row, 10)
    memory_id = _uuid(values[0])
    source_kind = MemorySourceKind(str(values[1]))
    return MemoryRecord(
        memory_id=MemoryId(memory_id, _optional_integer(values[6])),
        owner_user_id=_integer(values[3]),
        provenance=MemoryProvenance(
            conversation_id=ConversationId(str(values[2])),
            source_kind=source_kind,
            source_id=_uuid(values[8]),
            source_digest=str(values[4]),
        ),
        snapshot=_snapshot(values[5]),
        summary=None if values[7] is None else str(values[7]),
        created_at=_datetime(values[9]),
    )


def _snapshot(value: object) -> tuple[JsonObject, ...]:
    """@brief 解析 JSON snapshot / Parse a JSON snapshot.

    @param value driver JSON value / Driver JSON value.
    @return JSON object tuple / JSON-object tuple.
    """

    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Sequence) or isinstance(decoded, str):
        raise TypeError("Memory snapshot must be a JSON array")
    return tuple(_json_object(item) for item in decoded)


def _json_object(value: object) -> JsonObject:
    """@brief 校验 JSON object / Validate a JSON object.

    @param value 未知 JSON 值 / Unknown JSON value.
    @return JSON object / JSON object.
    """

    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("Memory snapshot item must be a JSON object")
    return cast(JsonObject, value)


def _row_values(row: object, expected: int) -> Sequence[object]:
    """@brief 读取并校验 row tuple / Read and validate a row tuple.

    @param row SQLAlchemy row / SQLAlchemy row.
    @param expected 期望列数 / Expected column count.
    @return row values / Row values.
    """

    if not isinstance(row, Sequence) or isinstance(row, str) or len(row) != expected:
        raise TypeError(f"Expected a {expected}-column memory row")
    return cast(Sequence[object], row)


def _validate_owner(owner_user_id: int) -> None:
    """@brief 校验 memory owner / Validate a memory owner.

    @param owner_user_id 所有者 / Owning user.
    @return None / None.
    """

    if isinstance(owner_user_id, bool) or owner_user_id <= 0:
        raise ValueError("Memory owner_user_id must be positive")


def _uuid(value: object) -> UUID:
    """@brief 转换 UUID / Convert a UUID.

    @param value driver value / Driver value.
    @return UUID / UUID.
    """

    return value if isinstance(value, UUID) else UUID(str(value))


def _integer(value: object) -> int:
    """@brief 转换整数 / Convert an integer.

    @param value driver value / Driver value.
    @return integer / Integer.
    """

    return int(str(value))


def _optional_integer(value: object) -> int | None:
    """@brief 转换可选整数 / Convert an optional integer.

    @param value driver value / Driver value.
    @return optional integer / Optional integer.
    """

    return None if value is None else _integer(value)


def _datetime(value: object) -> datetime:
    """@brief 转换 datetime / Convert a datetime.

    @param value driver value / Driver value.
    @return datetime / Datetime.
    """

    if not isinstance(value, datetime):
        raise TypeError("Memory timestamp must be datetime")
    return value


__all__ = ["PostgresMemoryReader"]
