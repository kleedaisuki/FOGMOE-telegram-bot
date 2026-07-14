"""@brief PostgreSQL Conversation reset 的无计费存储契约测试 / No-charge storage-contract tests for PostgreSQL Conversation reset."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from fogmoe_bot.domain.conversation.identity import ConversationId, TurnId
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_reset import (
    PostgresConversationResetUoW,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 reset 时刻 / Fixed reset instant."""

TURN_ID = TurnId.parse("11111111-1111-4111-8111-111111111111")
"""@brief 固定活动 Turn ID / Fixed active Turn identifier."""


def test_reset_cancels_active_workflow_without_legacy_billing_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief reset 只围栏活动与 Turn，不读取已删除的计费预留 / Reset fences only activities and Turns without reading deleted billing reservations.

    @param monkeypatch pytest 替换数据库原语 / pytest replacement utility.
    """

    async def scenario() -> None:
        """@brief 执行单个活动 Turn 的 reset 围栏 / Execute reset fencing for one active Turn.

        @return None / None.
        """

        reads: list[str] = []
        writes: list[str] = []

        async def fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> list[tuple[object, ...]]:
            """@brief 返回一个待取消的活动 / Return one activity pending cancellation.

            @param sql SQL 文本 / SQL text.
            @param params SQL 参数 / SQL parameters.
            @param connection 调用方连接 / Caller connection.
            @return 活动行 / Activity row.
            """

            del params, connection
            reads.append(sql)
            return [("activity-1", str(TURN_ID), "pending")]

        async def fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 返回行锁后的等待推理 Turn / Return the row-locked waiting-inference Turn.

            @param sql SQL 文本 / SQL text.
            @param params SQL 参数 / SQL parameters.
            @param connection 调用方连接 / Caller connection.
            @return Turn 行 / Turn row.
            """

            del params, connection
            reads.append(sql)
            return (str(TURN_ID), "waiting_inference", NOW)

        async def execute(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> int:
            """@brief 记录活动或 Turn 的取消更新 / Record an activity-or-Turn cancellation update.

            @param sql SQL 文本 / SQL text.
            @param params SQL 参数 / SQL parameters.
            @param connection 调用方连接 / Caller connection.
            @return 受影响行数 / Affected row count.
            """

            del params, connection
            writes.append(sql)
            return 1

        monkeypatch.setattr(db_connection, "fetch_all", fetch_all)
        monkeypatch.setattr(db_connection, "fetch_one", fetch_one)
        monkeypatch.setattr(db_connection, "execute", execute)
        persistence = PostgresConversationResetUoW(outbox=object())  # type: ignore[arg-type]

        await persistence._cancel_active_inference_turns(
            ConversationId("assistant-user:42"),
            cancelled_at=NOW,
            connection=object(),  # type: ignore[arg-type]
        )

        assert len(reads) == 2
        assert len(writes) == 2
        assert all("assistant.billing_reservations" not in sql for sql in reads)
        assert any("status = 'cancelled'" in sql for sql in writes)
        assert any("state = 'cancelled'" in sql for sql in writes)

    asyncio.run(scenario())
