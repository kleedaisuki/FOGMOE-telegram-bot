"""@brief Assistant tool checkpoint/receipt 的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for Assistant tool checkpoints and receipts."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.application.assistant.completion import (
    AgentStepCheckpoint,
    AssistantCompletion,
    CompletionToolCall,
)
from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectConflictError,
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
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
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


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
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def test_checkpoint_receipt_replay_conflict_and_kill_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief checkpoint 可重放，mutation 精确一次，故障窗口回滚 / Checkpoints replay, mutations happen once, and the kill window rolls back.

    @param monkeypatch 临时数据库配置 / Temporary database configuration.
    """

    async def scenario() -> None:
        """@brief 执行真实数据库场景 / Execute the real-database scenario."""

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        suffix = uuid4().hex
        user_id = 6_000_000_000_000_000_000 + int(suffix[:12], 16)
        turn_id = TurnId.new()
        conversation_id = ConversationId(f"assistant-tool-test:{suffix}")
        external = _External()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=external,
            generated_media=external,
            stickers=external,
            outbox=PostgresOutboxRepository(),
            recall=external,
            groups=PostgresGroupMessageProjection(),
        )
        context = ToolExecutionContext(
            turn_id=turn_id,
            conversation_id=conversation_id,
            delivery_stream_id=DeliveryStreamId(f"telegram:test:{suffix}"),
            user_id=user_id,
            chat_id=user_id,
            is_group=False,
            group_id=None,
            message_id=1,
        )
        request = ToolEffectRequest(
            context=context,
            invocation_id="step:0:call:0",
            provider_call_id="provider-call",
            tool_name="update_impression",
            effect_kind="account.update_impression",
            mutating=True,
            arguments={"impression": "curious"},
            request_hash="a" * 64,
        )
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users (id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                    "VALUES (%s, %s, 'telegram', %s, 0, 0, 'free')",
                    (user_id, user_id, f"tool_{suffix}"),
                    connection=connection,
                )
                await db_connection.execute(
                    "INSERT INTO conversation.conversation_turns "
                    "(turn_id, conversation_id, source_kind, source_key, source_update_id, state) "
                    "VALUES (CAST(%s AS UUID), %s, 'test.tool', %s, NULL, 'waiting_inference')",
                    (str(turn_id), str(conversation_id), suffix),
                    connection=connection,
                )

            store = PostgresAssistantToolStore(operations=operations)
            checkpoint = AgentStepCheckpoint(
                turn_id=turn_id,
                step_no=0,
                request_hash="b" * 64,
                route_key="test:model",
                completion=AssistantCompletion(
                    "",
                    {"role": "assistant", "content": ""},
                    (
                        CompletionToolCall(
                            "provider-call", "update_impression", request.arguments
                        ),
                    ),
                ),
            )
            assert await store.save_step(checkpoint) == checkpoint
            assert await store.save_step(checkpoint) == checkpoint
            assert await store.load_step(turn_id, 0) == checkpoint

            first = await store.execute(request)
            replay = await store.execute(request)
            assert first.replayed is False
            assert replay.replayed is True
            assert replay.result == first.result
            row = await db_connection.fetch_one(
                "SELECT impression FROM assistant.ai_user_affection WHERE user_id = %s",
                (user_id,),
            )
            assert row is not None and row[0] == "curious"
            receipt = await db_connection.fetch_one(
                "SELECT status, attempt_count FROM assistant.tool_effect_receipts "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s",
                (str(turn_id), request.invocation_id),
            )
            assert receipt is not None and tuple(receipt) == ("succeeded", 1)

            conflicting = ToolEffectRequest(
                context=context,
                invocation_id=request.invocation_id,
                provider_call_id=request.provider_call_id,
                tool_name=request.tool_name,
                effect_kind=request.effect_kind,
                mutating=True,
                arguments={"impression": "different"},
                request_hash="c" * 64,
            )
            with pytest.raises(ToolEffectConflictError):
                await store.execute(conflicting)

            second_request = ToolEffectRequest(
                context=context,
                invocation_id="step:1:call:0",
                provider_call_id="provider-call-2",
                tool_name="update_impression",
                effect_kind="account.update_impression",
                mutating=True,
                arguments={"impression": "after-recovery"},
                request_hash="d" * 64,
            )

            async def kill_after_mutation(
                effect: ToolEffectRequest,
                result: object,
            ) -> None:
                """@brief 模拟 mutation 后 kill / Simulate a kill after mutation.

                @param effect effect request / Effect request.
                @param result operation result / Operation result.
                """

                del effect, result
                raise RuntimeError("kill window")

            failing = PostgresAssistantToolStore(
                operations=operations,
                after_operation=kill_after_mutation,  # type: ignore[arg-type]
            )
            with pytest.raises(RuntimeError, match="kill window"):
                await failing.execute(second_request)
            unchanged = await db_connection.fetch_one(
                "SELECT impression FROM assistant.ai_user_affection WHERE user_id = %s",
                (user_id,),
            )
            assert unchanged is not None and unchanged[0] == "curious"
            pending = await db_connection.fetch_one(
                "SELECT status, claim_token FROM assistant.tool_effect_receipts "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s",
                (str(turn_id), second_request.invocation_id),
            )
            assert pending is not None and tuple(pending) == ("pending", None)

            recovered = await store.execute(second_request)
            assert recovered.replayed is False
            final = await db_connection.fetch_one(
                "SELECT impression FROM assistant.ai_user_affection WHERE user_id = %s",
                (user_id,),
            )
            assert final is not None and final[0] == "after-recovery"

            cancelled_request = ToolEffectRequest(
                context=context,
                invocation_id="step:2:call:0",
                provider_call_id="provider-call-3",
                tool_name="update_impression",
                effect_kind="account.update_impression",
                mutating=True,
                arguments={"impression": "must-not-commit"},
                request_hash="e" * 64,
            )
            entered_window = asyncio.Event()

            async def wait_for_kill(
                effect: ToolEffectRequest,
                result: object,
            ) -> None:
                """@brief 保持 mutation transaction 直到取消 / Hold the mutation transaction until cancellation.

                @param effect effect request / Effect request.
                @param result operation result / Operation result.
                """

                del effect, result
                entered_window.set()
                await asyncio.Event().wait()

            cancellable = PostgresAssistantToolStore(
                operations=operations,
                after_operation=wait_for_kill,  # type: ignore[arg-type]
            )
            task = asyncio.create_task(cancellable.execute(cancelled_request))
            await entered_window.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            leased = await db_connection.fetch_one(
                "SELECT status, claim_token IS NOT NULL, lease_expires_at IS NOT NULL "
                "FROM assistant.tool_effect_receipts WHERE turn_id = CAST(%s AS UUID) "
                "AND invocation_id = %s",
                (str(turn_id), cancelled_request.invocation_id),
            )
            assert leased is not None and tuple(leased) == ("processing", True, True)
            still_recovered = await db_connection.fetch_one(
                "SELECT impression FROM assistant.ai_user_affection WHERE user_id = %s",
                (user_id,),
            )
            assert (
                still_recovered is not None and still_recovered[0] == "after-recovery"
            )
            await db_connection.execute(
                "UPDATE assistant.tool_effect_receipts "
                "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s",
                (str(turn_id), cancelled_request.invocation_id),
            )
            reclaimed = await store.execute(cancelled_request)
            assert reclaimed.replayed is False
            after_reclaim = await db_connection.fetch_one(
                "SELECT impression FROM assistant.ai_user_affection WHERE user_id = %s",
                (user_id,),
            )
            assert after_reclaim is not None and after_reclaim[0] == "must-not-commit"
            recovered_receipt = await db_connection.fetch_one(
                "SELECT status, attempt_count, claim_token FROM assistant.tool_effect_receipts "
                "WHERE turn_id = CAST(%s AS UUID) AND invocation_id = %s",
                (str(turn_id), cancelled_request.invocation_id),
            )
            assert recovered_receipt is not None
            assert tuple(recovered_receipt) == ("succeeded", 2, None)
        finally:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "DELETE FROM conversation.conversation_turns WHERE turn_id = CAST(%s AS UUID)",
                    (str(turn_id),),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM assistant.ai_user_affection WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_diary_schedule_and_kindness_share_atomic_receipt_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三类业务 mutation 与 succeeded receipt 原子提交且重放不重复。"""

    async def scenario() -> None:
        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
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
            recall=external,
            groups=PostgresGroupMessageProjection(),
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
                "timestamp_utc": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "recurrence_unit": "none",
                "recurrence_interval": 1,
                "trigger_reason": "contract test",
                "instruction": "say hello",
            },
            request_hash="b" * 64,
        )
        kindness = request(
            invocation_id="step:0:call:2",
            tool_name="kindness_gift",
            effect_kind="account.kindness_gift",
            arguments={"amount": 4},
            request_hash="c" * 64,
        )
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                    "VALUES (%s, %s, 'telegram', %s, 0, 0, 'free')",
                    (user_id, user_id, f"atomic_{suffix}"),
                    connection=connection,
                )
                await db_connection.execute(
                    "INSERT INTO conversation.conversation_turns "
                    "(turn_id, conversation_id, source_kind, source_key, "
                    "source_update_id, state) VALUES "
                    "(CAST(%s AS UUID), %s, 'test.tool', %s, NULL, "
                    "'waiting_inference')",
                    (str(turn_id), str(conversation_id), suffix),
                    connection=connection,
                )

            store = PostgresAssistantToolStore(operations=operations)
            for operation in (diary, schedule, kindness):
                assert (
                    operations.transaction_mode(operation)
                    is ToolTransactionMode.ATOMIC_MUTATION
                )
                first = await store.execute(operation)
                replay = await store.execute(operation)
                assert first.replayed is False
                assert replay.replayed is True
                assert replay.result == first.result

            diary_row = await db_connection.fetch_one(
                "SELECT content FROM conversation.ai_user_diary_pages "
                "WHERE user_id = %s AND page_no = 1",
                (user_id,),
            )
            assert diary_row is not None and diary_row[0] == "durable note"
            schedule_row = await db_connection.fetch_one(
                "SELECT status, trigger_reason, prompt FROM assistant.ai_schedules "
                "WHERE user_id = %s",
                (user_id,),
            )
            assert schedule_row is not None
            assert tuple(schedule_row) == ("pending", "contract test", "say hello")
            account_row = await db_connection.fetch_one(
                "SELECT coins FROM identity.users WHERE id = %s",
                (user_id,),
            )
            assert account_row is not None and account_row[0] == 4
            gift_rows = await db_connection.fetch_all(
                "SELECT amount FROM economy.kindness_gifts WHERE recipient_id = %s",
                (user_id,),
            )
            assert [int(row[0]) for row in gift_rows] == [4]
            receipts = await db_connection.fetch_all(
                "SELECT status, attempt_count FROM assistant.tool_effect_receipts "
                "WHERE turn_id = CAST(%s AS UUID) ORDER BY invocation_id",
                (str(turn_id),),
            )
            assert [tuple(row) for row in receipts] == [
                ("succeeded", 1),
                ("succeeded", 1),
                ("succeeded", 1),
            ]
        finally:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "DELETE FROM conversation.conversation_turns "
                    "WHERE turn_id = CAST(%s AS UUID)",
                    (str(turn_id),),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM conversation.ai_user_diary_pages WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM assistant.ai_schedules WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM economy.kindness_gifts WHERE recipient_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
