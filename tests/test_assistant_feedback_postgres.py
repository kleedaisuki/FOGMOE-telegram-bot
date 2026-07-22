"""@brief Assistant feedback 的真实 PostgreSQL 契约测试 / Real-PostgreSQL contract test for Assistant feedback."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from observability_testkit import make_telemetry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.standalone_outbound import (
    PostgresStandaloneOutboundCapability,
)


class ConnectionBoundStandaloneRepository:
    """@brief 把 standalone primitive 绑定到测试外层事务 / Bind the standalone primitive to the test's outer transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        """@brief 保存真实 PostgreSQL 连接 / Store the real PostgreSQL connection.

        @param connection 活动事务连接 / Connection with an active transaction.
        """

        self._connection = connection
        """@brief 活动测试事务 / Active test transaction."""
        self._repository = PostgresOutboxRepository()
        """@brief 真实 outbox 仓储 / Real outbox repository."""

    async def enqueue_standalone_outbound(
        self,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 在外层事务内调用真实 primitive / Invoke the real primitive in the outer transaction.

        @param draft standalone outbox 草稿 / Standalone outbox draft.
        @return 真实仓储回执 / Real repository receipt.
        """

        return await self._repository.enqueue_standalone_outbound_in_transaction(
            self._connection,
            draft,
        )


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN / Read an explicit test DSN.

    @return SQLAlchemy asyncpg URL / SQLAlchemy asyncpg URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def test_real_postgres_feedback_is_idempotent_conflict_safe_and_rolled_back() -> None:
    """@brief 真实 PG 中反馈可重放、异语义冲突且外层失败无残留 / On real PostgreSQL, feedback replays, conflicts safely, and leaves no residue after outer rollback."""

    async def scenario() -> None:
        """@brief 执行真实事务场景 / Execute the real transaction scenario.

        @return None / None.
        """

        engine = create_async_engine(_postgres_url(), poolclass=NullPool)
        suffix = uuid4().hex
        conversation_id = ConversationId(f"assistant-pg-test:{suffix}")
        idempotency_key = f"update:{suffix}:assistant-feedback:text_too_long"
        command = StandaloneOutboundCommand(
            conversation_id=conversation_id,
            delivery_stream_id=DeliveryStreamId(
                f"telegram:primary:pg-test:{suffix}:thread:0"
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={"chat_id": 42, "text": "too long"},
            idempotency_key=idempotency_key,
            created_at=datetime.now(UTC),
        )
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    capability = PostgresStandaloneOutboundCapability(
                        repository=ConnectionBoundStandaloneRepository(connection),
                        telemetry=make_telemetry(),
                    )
                    await capability.enqueue(command)
                    await capability.enqueue(command)

                    conflicting = StandaloneOutboundCommand(
                        conversation_id=command.conversation_id,
                        delivery_stream_id=command.delivery_stream_id,
                        kind=command.kind,
                        payload={"chat_id": 42, "text": "different"},
                        idempotency_key=command.idempotency_key,
                        created_at=command.created_at,
                    )
                    with pytest.raises(
                        IdempotencyConflictError,
                        match="different semantics",
                    ):
                        await capability.enqueue(conflicting)

                    row = (
                        await connection.execute(
                            text(
                                "SELECT message_id, turn_id, COUNT(*) OVER () "
                                "FROM conversation.outbound_messages "
                                "WHERE conversation_id = :conversation_id "
                                "AND idempotency_key = :idempotency_key"
                            ),
                            {
                                "conversation_id": str(command.conversation_id),
                                "idempotency_key": command.idempotency_key,
                            },
                        )
                    ).one()
                    assert (
                        row[0]
                        == OutboundMessageId.for_conversation(
                            command.conversation_id,
                            command.idempotency_key,
                        ).value
                    )
                    assert row[1] is None
                    assert row[2] == 1
                finally:
                    await transaction.rollback()

            async with engine.connect() as probe:
                remaining = await probe.scalar(
                    text(
                        "SELECT COUNT(*) FROM conversation.outbound_messages "
                        "WHERE conversation_id = :conversation_id "
                        "AND idempotency_key = :idempotency_key"
                    ),
                    {
                        "conversation_id": str(command.conversation_id),
                        "idempotency_key": command.idempotency_key,
                    },
                )
                assert remaining == 0
                await probe.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
