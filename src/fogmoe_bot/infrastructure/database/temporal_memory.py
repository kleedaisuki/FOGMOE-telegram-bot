"""@brief 基于既有 retrieval projection 的时间历史读取 / Temporal-history reads over the existing retrieval projection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fogmoe_bot.application.assistant.temporal_memory import (
    TemporalMemoryPassage,
    TemporalMemoryQuery,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresTemporalMemoryReader:
    """@brief 只读 passage 时间索引且不介入 Memory pipeline / Read the passage time index without participating in the Memory pipeline."""

    def __init__(self, *, corpus_id: str, format_version: int) -> None:
        """@brief 固定目标 corpus 与 renderer 版本 / Pin the target corpus and renderer version.

        @param corpus_id 既有历史语料库 / Existing historical corpus.
        @param format_version passage renderer 版本 / Passage-renderer version.
        """

        normalized_corpus = corpus_id.strip()
        if not normalized_corpus or len(normalized_corpus) > 100:
            raise ValueError("Temporal Memory corpus_id must contain 1-100 characters")
        if isinstance(format_version, bool) or format_version < 1:
            raise ValueError("Temporal Memory format_version must be positive")
        self._corpus_id = normalized_corpus
        self._format_version = format_version

    async def search(
        self, query: TemporalMemoryQuery
    ) -> tuple[TemporalMemoryPassage, ...]:
        """@brief 用现有复合索引读取区间或定点 passages / Read interval or point passages using the existing composite index.

        @param query 强租户、UTC 查询 / Strongly tenant-scoped UTC query.
        @return 按 latest 或 nearest 稳定排序的 passages / Passages stably ordered by latest or nearest.
        @note 查询不连接 vector 表，也不调用 embedding provider。/
            The query neither joins the vector table nor calls an embedding provider.
        """

        distance_sql = "NULL::DOUBLE PRECISION"
        parameters: list[object] = []
        if query.nearest_to is not None:
            distance_sql = "ABS(EXTRACT(EPOCH FROM (occurred_at - %s)))"
            parameters.append(query.nearest_to)
        sql = (
            "SELECT passage_id, source_kind, source_id, occurred_at, content_text, "
            f"{distance_sql} AS temporal_distance_seconds "
            "FROM retrieval.passages WHERE scope_kind = %s AND scope_id = %s "
            "AND corpus_id = %s AND format_version = %s "
            "AND occurred_at >= %s AND occurred_at < %s "
        )
        parameters.extend(
            (
                query.scope.kind,
                query.scope.scope_id,
                self._corpus_id,
                self._format_version,
                query.occurred_within.start,
                query.occurred_within.end,
            )
        )
        if query.nearest_to is None:
            sql += "ORDER BY occurred_at DESC, passage_id ASC LIMIT %s"
        else:
            sql += (
                "ORDER BY temporal_distance_seconds ASC, occurred_at DESC, "
                "passage_id ASC LIMIT %s"
            )
        parameters.append(query.limit)
        rows = await db_connection.fetch_all(sql, tuple(parameters))
        return tuple(_map_passage(row) for row in rows)


def _map_passage(row: object) -> TemporalMemoryPassage:
    """@brief 映射稳定六列读模型 / Map the stable six-column read model.

    @param row 数据库行 / Database row.
    @return 时间历史 passage / Temporal-history passage.
    """

    values: tuple[object, ...] = tuple(row)  # type: ignore[arg-type]
    if len(values) != 6:
        raise ValueError("Temporal Memory query returned an unexpected row shape")
    occurred_at = values[3]
    if not isinstance(occurred_at, datetime):
        raise TypeError("Temporal Memory occurred_at must be a datetime")
    distance = values[5]
    return TemporalMemoryPassage(
        passage_id=_uuid(values[0]),
        source_kind=str(values[1]),
        source_id=_uuid(values[2]),
        occurred_at=occurred_at,
        content=str(values[4]),
        temporal_distance_seconds=(None if distance is None else _float(distance)),
    )


def _uuid(value: object) -> UUID:
    """@brief 规范数据库 UUID / Normalize a database UUID.

    @param value UUID 或文本 / UUID or text.
    @return UUID / UUID.
    """

    return value if isinstance(value, UUID) else UUID(str(value))


def _float(value: object) -> float:
    """@brief 规范数据库数值 / Normalize a database numeric value.

    @param value 数值或文本 / Numeric value or text.
    @return float / Float.
    @raise TypeError 数据库返回非数值时抛出 / Raised when the database returns a non-numeric value.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise TypeError("Temporal Memory distance must be numeric")
    return float(value)


__all__ = ["PostgresTemporalMemoryReader"]
