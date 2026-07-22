"""@brief 独立时间 Memory PostgreSQL reader 契约测试 / PostgreSQL-reader contracts for standalone temporal Memory."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fogmoe_bot.application.assistant.temporal_memory import TemporalMemoryQuery
from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.domain.temporal import UtcInterval
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.temporal_memory import (
    PostgresTemporalMemoryReader,
)


ANCHOR = datetime(2034, 5, 6, 7, 8, tzinfo=UTC)
"""@brief SQL 参数化测试的固定锚点 / Fixed anchor for SQL-parameterization tests."""


def test_interval_reader_filters_half_open_before_latest_limit(monkeypatch) -> None:
    """@brief Reader 在 LIMIT 前使用 UTC 半开区间并按最新稳定排序 / The reader applies a UTC half-open interval before LIMIT and orders latest-first."""

    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch_all(
        sql: str,
        parameters: tuple[object, ...],
        *,
        connection=None,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获 SQL 并返回一条 provenance row / Capture SQL and return one provenance row.

        @param sql 参数化 SQL / Parameterized SQL.
        @param parameters 绑定参数 / Bound parameters.
        @param connection 可选连接 / Optional connection.
        @return 固定数据库行 / Fixed database row.
        """

        assert connection is None
        calls.append((sql, parameters))
        return [
            (
                UUID("00000000-0000-0000-0000-000000000060"),
                "conversation.turn",
                UUID("00000000-0000-0000-0000-000000000061"),
                ANCHOR,
                "bounded history",
                None,
            )
        ]

    async def scenario() -> None:
        """@brief 执行普通区间查询 / Execute a plain interval query."""

        monkeypatch.setattr(db_connection, "fetch_all", fetch_all)
        window = UtcInterval(
            ANCHOR - timedelta(hours=2),
            ANCHOR + timedelta(hours=2),
        )
        reader = PostgresTemporalMemoryReader(
            corpus_id="conversation.episodic",
            format_version=3,
        )

        passages = await reader.search(
            TemporalMemoryQuery(
                scope=RetrievalScope("personal", 7),
                occurred_within=window,
                limit=4,
            )
        )

        assert len(calls) == 1
        sql, parameters = calls[0]
        assert "occurred_at >= %s AND occurred_at < %s" in sql
        assert "ORDER BY occurred_at DESC, passage_id ASC LIMIT %s" in sql
        assert "passage_vectors" not in sql and "embedding" not in sql
        assert parameters == (
            "personal",
            7,
            "conversation.episodic",
            3,
            window.start,
            window.end,
            4,
        )
        assert passages[0].passage_id == UUID("00000000-0000-0000-0000-000000000060")
        assert passages[0].source_id == UUID("00000000-0000-0000-0000-000000000061")
        assert passages[0].occurred_at == ANCHOR
        assert passages[0].content == "bounded history"
        assert passages[0].temporal_distance_seconds is None

    asyncio.run(scenario())


def test_around_reader_uses_all_stable_nearest_tie_breakers(monkeypatch) -> None:
    """@brief 定点 SQL 按绝对距离、较新时间和 passage ID 依次破平局 / Point SQL breaks ties by distance, newer time, then passage ID."""

    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch_all(
        sql: str,
        parameters: tuple[object, ...],
        *,
        connection=None,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获定点 SQL 并返回已排序 rows / Capture point SQL and return preordered rows.

        @param sql 参数化 SQL / Parameterized SQL.
        @param parameters 绑定参数 / Bound parameters.
        @param connection 可选连接 / Optional connection.
        @return 两条等距历史记录 / Two equidistant historical rows.
        """

        assert connection is None
        calls.append((sql, parameters))
        return [
            (
                UUID("00000000-0000-0000-0000-000000000070"),
                "conversation.turn",
                UUID("00000000-0000-0000-0000-000000000071"),
                ANCHOR + timedelta(seconds=30),
                "newer equidistant passage",
                30,
            ),
            (
                UUID("00000000-0000-0000-0000-000000000072"),
                "conversation.turn",
                UUID("00000000-0000-0000-0000-000000000073"),
                ANCHOR - timedelta(seconds=30),
                "older equidistant passage",
                30,
            ),
        ]

    async def scenario() -> None:
        """@brief 执行定点检索 / Execute a point retrieval."""

        monkeypatch.setattr(db_connection, "fetch_all", fetch_all)
        window = UtcInterval.around(ANCHOR, timedelta(minutes=10))
        reader = PostgresTemporalMemoryReader(
            corpus_id="conversation.episodic",
            format_version=1,
        )

        passages = await reader.search(
            TemporalMemoryQuery(
                scope=RetrievalScope("group", -10042),
                occurred_within=window,
                nearest_to=ANCHOR,
                limit=2,
            )
        )

        assert len(calls) == 1
        sql, parameters = calls[0]
        assert "ABS(EXTRACT(EPOCH FROM (occurred_at - %s)))" in sql
        assert (
            "ORDER BY temporal_distance_seconds ASC, occurred_at DESC, "
            "passage_id ASC LIMIT %s"
        ) in sql
        assert parameters == (
            ANCHOR,
            "group",
            -10042,
            "conversation.episodic",
            1,
            window.start,
            window.end,
            2,
        )
        assert [passage.content for passage in passages] == [
            "newer equidistant passage",
            "older equidistant passage",
        ]
        assert [passage.temporal_distance_seconds for passage in passages] == [
            30.0,
            30.0,
        ]

    asyncio.run(scenario())
