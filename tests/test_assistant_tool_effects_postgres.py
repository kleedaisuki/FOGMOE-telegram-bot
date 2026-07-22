"""@brief Assistant tool checkpoint/receipt 的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for Assistant tool checkpoints and receipts."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from postgres_test_support import configure_bot_database

from fogmoe_bot.application.assistant.temporal_memory import TemporalMemoryReader
from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.scheduling.service import SchedulingService
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.temporal import UTC_TIME_ZONE
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    PostgresAssistantToolStore,
    ToolTransactionMode,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)


class _External:
    """@brief 本测试不应调用的外部 adapter / External adapter that must not be called here."""

    async def execute(self, request: ToolEffectRequest):
        """@brief 拒绝调用 / Reject invocation."""

        raise AssertionError(request.tool_name)

    async def generate(self, request: ToolEffectRequest):
        """@brief 拒绝调用 / Reject invocation."""

        raise AssertionError(request.tool_name)

    async def list_packs(self, pack_name: str | None):
        """@brief 拒绝调用 / Reject invocation."""

        raise AssertionError(pack_name)


def _postgres_url() -> str:
    """@brief 读取真实 PostgreSQL DSN / Read a real PostgreSQL DSN.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def test_diary_and_schedule_share_atomic_receipt_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 日记与日程共用原子回执 / Diary and schedule share atomic receipts.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    async def scenario() -> None:
        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        suffix = uuid4().hex
        user_id = 6_100_000_000_000_000_000 + int(suffix[:12], 16)
        turn_id = TurnId.new()
        conversation_id = ConversationId(f"assistant-tool-atomic:{suffix}")
        external = _External()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=external,
            generated_media=external,
            stickers=external,
            outbox=PostgresOutboxRepository(),
            memory=external,
            temporal_memory=cast(TemporalMemoryReader, external),
            groups=PostgresGroupMessageProjection(),
            time=TimeService(default_time_zone=UTC_TIME_ZONE),
            scheduling=SchedulingService(),
        )
        context = ToolExecutionContext(
            turn_id=turn_id,
            conversation_id=conversation_id,
            delivery_stream_id=DeliveryStreamId(f"telegram:test:{suffix}"),
            user_id=user_id,
            chat_id=user_id,
            is_group=False,
            group_id=None,
            message_id=2,
        )

        def request(
            *,
            invocation_id: str,
            tool_name: str,
            effect_kind: str,
            arguments: JsonObject,
            request_hash: str,
        ) -> ToolEffectRequest:
            return ToolEffectRequest(
                context=context,
                invocation_id=invocation_id,
                provider_call_id=f"provider-{invocation_id}",
                tool_name=tool_name,
                effect_kind=effect_kind,
                mutating=True,
                arguments=arguments,
                request_hash=request_hash,
            )

        diary = request(
            invocation_id="step:0:call:0",
            tool_name="user_diary",
            effect_kind="diary.append",
            arguments={"action": "append", "page": 1, "content": "durable note"},
            request_hash="a" * 64,
        )
        schedule = request(
            invocation_id="step:0:call:1",
            tool_name="schedule_ai_message",
            effect_kind="schedule.create",
            arguments={
                "action": "create",
                "cadence": {
                    "kind": "one_shot",
                    "first_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                },
                "trigger_reason": "contract test",
                "instruction": "say hello",
            },
            request_hash="b" * 64,
        )
        try:
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"atomic_{suffix}"),
                    connection=connection,
                )
                await db.execute(
                    "INSERT INTO conversation.conversation_turns "
                    "(turn_id, conversation_id, source_kind, source_key, "
                    "source_update_id, state) VALUES "
                    "(CAST(%s AS UUID), %s, 'test.tool', %s, NULL, "
                    "'waiting_inference')",
                    (str(turn_id), str(conversation_id), suffix),
                    connection=connection,
                )

            store = PostgresAssistantToolStore(operations=operations)
            for operation in (diary, schedule):
                assert (
                    operations.transaction_mode(operation)
                    is ToolTransactionMode.ATOMIC_MUTATION
                )
                first = await store.execute(operation)
                replay = await store.execute(operation)
                assert first.replayed is False
                assert replay.replayed is True
                assert replay.result == first.result
            diary_row = await db.fetch_one(
                "SELECT content FROM conversation.ai_user_diary_pages "
                "WHERE user_id = %s AND page_no = 1",
                (user_id,),
            )
            assert diary_row is not None and diary_row[0] == "durable note"
            schedule_row = await db.fetch_one(
                "SELECT status, trigger_reason, instruction "
                "FROM scheduling.assistant_schedules WHERE creator_user_id = %s",
                (user_id,),
            )
            assert schedule_row is not None
            assert tuple(schedule_row) == ("pending", "contract test", "say hello")
            bank_entries = await db.fetch_one(
                "SELECT count(*) FROM bank.ledger_entries WHERE actor_id = %s",
                (user_id,),
            )
            assert bank_entries is not None and bank_entries[0] == 0
            receipts = await db.fetch_all(
                "SELECT status, attempt_count FROM assistant.tool_effect_receipts "
                "WHERE turn_id = CAST(%s AS UUID) ORDER BY invocation_id",
                (str(turn_id),),
            )
            assert [tuple(row) for row in receipts] == [
                ("succeeded", 1),
                ("succeeded", 1),
            ]
        finally:
            async with db.transaction() as connection:
                await db.execute(
                    "DELETE FROM conversation.conversation_turns "
                    "WHERE turn_id = CAST(%s AS UUID)",
                    (str(turn_id),),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM conversation.ai_user_diary_pages WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM scheduling.assistant_schedules "
                    "WHERE creator_user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
