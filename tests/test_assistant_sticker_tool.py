"""Durable Assistant outbound tools 的 receipt/outbox 契约测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from types import TracebackType
from typing import cast

from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncConnection

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
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_ARTIFACT,
    SEND_TELEGRAM_STICKER,
    OutboundDraft,
)
from fogmoe_bot.domain.conversation.payloads import JsonValue
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
    StandaloneOutboxWriter,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)


class _UnusedAdapters:
    """@brief sticker mutation 不应访问的外部 adapters / External adapters a sticker mutation must not access."""

    async def execute(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 拒绝 external-read 调用 / Reject external-read calls.

        @param request 意外请求 / Unexpected request.
        @return 永不返回 / Never returns.
        """

        raise AssertionError(request.tool_name)

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 拒绝 media 调用 / Reject media calls.

        @param request 意外请求 / Unexpected request.
        @return 永不返回 / Never returns.
        """

        raise AssertionError(request.tool_name)

    async def list_packs(self, pack_name: str | None) -> JsonValue:
        """@brief 拒绝 catalog read / Reject catalog reads.

        @param pack_name 意外 pack / Unexpected pack.
        @return 永不返回 / Never returns.
        """

        raise AssertionError(pack_name)


class _WorkflowRecorder:
    """@brief 记录 standalone outbox 写入 / Record standalone-outbox writes."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.connections: list[AsyncConnection] = []
        """@brief outbox 写入事务 / Transactions used for outbox writes."""
        self.drafts: list[OutboundDraft] = []
        """@brief outbox 草稿 / Recorded outbox drafts."""

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> None:
        """@brief 捕获调用者事务与草稿 / Capture the caller transaction and draft.

        @param connection receipt 事务 / Receipt transaction.
        @param draft sticker outbox 草稿 / Sticker-outbox draft.
        @return None / None.
        """

        self.connections.append(connection)
        self.drafts.append(draft)


class _GeneratedMedia:
    """断言 generated-media operation 不在数据库事务内运行。"""

    def __init__(self, database: _ReceiptDatabase) -> None:
        self._database = database
        self.calls: list[ToolEffectRequest] = []

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        """返回一个 durable artifact reference，并记录 transaction depth。"""

        assert self._database.open_transactions == 0
        self.calls.append(request)
        return {
            "status": "generated",
            "artifacts": [
                {
                    "artifact_id": "artifact-42",
                    "kind": "image",
                    "filename": "answer.png",
                    "mime_type": "image/png",
                    "size_bytes": 321,
                }
            ],
        }


class _Transaction:
    """@brief 最小 async transaction 替身 / Minimal asynchronous transaction double."""

    def __init__(
        self,
        connection: AsyncConnection,
        database: _ReceiptDatabase,
    ) -> None:
        """@brief 保存连接 / Store the connection.

        @param connection 测试连接 identity / Test connection identity.
        """

        self._connection = connection
        """@brief 事务连接 / Transaction connection."""
        self._database = database

    async def __aenter__(self) -> AsyncConnection:
        """@brief 进入事务 / Enter the transaction.

        @return 连接 / Connection.
        """

        self._database.open_transactions += 1
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """@brief 离开事务 / Exit the transaction.

        @param exc_type 可选异常类型 / Optional exception type.
        @param exc_value 可选异常 / Optional exception.
        @param traceback 可选 traceback / Optional traceback.
        @return None / None.
        """

        del exc_type, exc_value, traceback
        self._database.open_transactions -= 1


class _ReceiptDatabase:
    """@brief 最小 receipt SQL 状态机 / Minimal receipt-SQL state machine."""

    def __init__(self) -> None:
        """@brief 初始化空 receipt / Initialize an empty receipt."""

        self.request_hash: str | None = None
        """@brief 规范请求摘要 / Canonical request digest."""
        self.status = ""
        """@brief receipt 状态 / Receipt status."""
        self.result: JsonValue = None
        """@brief 持久化结果 / Persisted result."""
        self.claim_token: str | None = None
        """@brief fencing token / Fencing token."""
        self.succeeded_connection: AsyncConnection | None = None
        """@brief 标记 succeeded 的事务 / Transaction that marked success."""
        self.transaction_connections: list[AsyncConnection] = []
        """@brief 已打开的事务 / Opened transactions."""
        self.open_transactions = 0

    def transaction(self) -> _Transaction:
        """@brief 创建带唯一 identity 的事务 / Create a transaction with unique identity.

        @return async transaction 替身 / Async transaction double.
        """

        connection = cast(AsyncConnection, object())
        self.transaction_connections.append(connection)
        return _Transaction(connection, self)

    async def execute(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        connection: AsyncConnection | None = None,
    ) -> int:
        """@brief 执行 receipt 状态转移 / Execute a receipt-state transition.

        @param sql SQL 模板 / SQL template.
        @param params SQL 参数 / SQL parameters.
        @param connection 调用者事务 / Caller transaction.
        @return 命中行数 / Affected row count.
        """

        if sql.startswith("INSERT INTO assistant.tool_effect_receipts"):
            if self.request_hash is None:
                self.request_hash = str(params[5])
                self.status = "pending"
            return 1
        if "SET status = 'processing'" in sql:
            self.status = "processing"
            self.claim_token = str(params[0])
            return 1
        if "SET status = 'succeeded'" in sql:
            self.status = "succeeded"
            self.result = cast(JsonValue, json.loads(str(params[0])))
            self.claim_token = None
            self.succeeded_connection = connection
            return 1
        if "SET status = 'pending'" in sql:
            self.status = "pending"
            self.claim_token = None
            return 1
        raise AssertionError(sql)

    async def fetch_one(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        connection: AsyncConnection | None = None,
    ) -> Sequence[object] | None:
        """@brief 读取 receipt 或校验 fencing token / Read a receipt or verify its fencing token.

        @param sql SQL 模板 / SQL template.
        @param params SQL 参数 / SQL parameters.
        @param connection 调用者事务 / Caller transaction.
        @return 模拟行或 None / Simulated row or None.
        """

        del connection
        if sql.startswith("SELECT request_hash, status, result"):
            return (
                self.request_hash,
                self.status,
                self.result,
                self.claim_token,
                None,
            )
        if sql.startswith("SELECT 1 FROM assistant.tool_effect_receipts"):
            return (1,) if self.claim_token == str(params[-1]) else None
        raise AssertionError(sql)


def test_receipt_replay_queues_exactly_one_standalone_sticker_outbox(
    monkeypatch: MonkeyPatch,
) -> None:
    """@brief succeeded receipt 重放不重复写 sticker outbox / Replaying a succeeded receipt does not duplicate the sticker outbox.

    @param monkeypatch receipt SQL 替身注入 / Receipt-SQL double injection.
    """

    async def scenario() -> None:
        """@brief 执行首次调用与重放 / Execute the first call and its replay."""

        database = _ReceiptDatabase()
        monkeypatch.setattr(
            db,
            "transaction",
            database.transaction,
        )
        monkeypatch.setattr(
            db,
            "execute",
            database.execute,
        )
        monkeypatch.setattr(
            db,
            "fetch_one",
            database.fetch_one,
        )
        workflow = _WorkflowRecorder()
        adapters = _UnusedAdapters()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=adapters,
            generated_media=adapters,
            stickers=adapters,
            outbox=cast(StandaloneOutboxWriter, workflow),
            memory=adapters,
            temporal_memory=cast(TemporalMemoryReader, adapters),
            groups=PostgresGroupMessageProjection(),
            time=TimeService(default_time_zone=UTC_TIME_ZONE),
            scheduling=SchedulingService(),
        )
        turn_id = TurnId.new()
        conversation_id = ConversationId("assistant-user:42")
        stream_id = DeliveryStreamId("telegram:primary:chat:-100:thread:11")
        request = ToolEffectRequest(
            context=ToolExecutionContext(
                turn_id=turn_id,
                conversation_id=conversation_id,
                delivery_stream_id=stream_id,
                user_id=42,
                chat_id=-100,
                is_group=True,
                group_id=-100,
                message_id=7,
                message_thread_id=11,
            ),
            invocation_id="step:2:call:0",
            provider_call_id="provider-sticker-call",
            tool_name="send_sticker",
            effect_kind="telegram.send_sticker",
            mutating=True,
            arguments={"pack_name": "WhiteWind", "emoji": "😊"},
            request_hash="a" * 64,
        )
        store = PostgresAssistantToolStore(operations=operations)

        first = await store.execute(request)
        replay = await store.execute(request)

        assert first.result == {
            "status": "queued",
            "pack_name": "WhiteWind",
            "emoji": "😊",
        }
        assert first.replayed is False
        assert replay.result == first.result
        assert replay.replayed is True
        assert len(workflow.drafts) == 1
        draft = workflow.drafts[0]
        expected_key = f"assistant-tool:{turn_id}:step:2:call:0:sticker"
        assert draft.message_id == OutboundMessageId.for_conversation(
            conversation_id,
            expected_key,
        )
        assert draft.conversation_id == conversation_id
        assert draft.turn_id is None
        assert draft.delivery_stream_id == stream_id
        assert draft.kind == SEND_TELEGRAM_STICKER
        assert draft.payload == {
            "chat_id": -100,
            "pack_name": "WhiteWind",
            "emoji": "😊",
            "message_thread_id": 11,
        }
        assert draft.idempotency_key == expected_key
        assert workflow.connections == [database.succeeded_connection]

    asyncio.run(scenario())


def test_generated_media_runs_outside_transaction_then_finalizes_once(
    monkeypatch: MonkeyPatch,
) -> None:
    """外部 media generation 不占用 DB transaction，artifact intent 与 receipt 原子落库。"""

    async def scenario() -> None:
        database = _ReceiptDatabase()
        monkeypatch.setattr(db, "transaction", database.transaction)
        monkeypatch.setattr(db, "execute", database.execute)
        monkeypatch.setattr(db, "fetch_one", database.fetch_one)
        outbox = _WorkflowRecorder()
        unused = _UnusedAdapters()
        generated = _GeneratedMedia(database)
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=unused,
            generated_media=generated,
            stickers=unused,
            outbox=cast(StandaloneOutboxWriter, outbox),
            memory=unused,
            temporal_memory=cast(TemporalMemoryReader, unused),
            groups=PostgresGroupMessageProjection(),
            time=TimeService(default_time_zone=UTC_TIME_ZONE),
            scheduling=SchedulingService(),
        )
        turn_id = TurnId.new()
        conversation_id = ConversationId("assistant-user:42")
        request = ToolEffectRequest(
            context=ToolExecutionContext(
                turn_id=turn_id,
                conversation_id=conversation_id,
                delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42"),
                user_id=42,
                chat_id=42,
                is_group=False,
                group_id=None,
                message_id=8,
            ),
            invocation_id="step:3:call:0",
            provider_call_id="provider-image-call",
            tool_name="generate_image",
            effect_kind="media.generate_image",
            mutating=True,
            arguments={"prompt": "Klee"},
            request_hash="b" * 64,
        )

        assert (
            operations.transaction_mode(request)
            is ToolTransactionMode.OUTSIDE_TRANSACTION
        )
        first = await PostgresAssistantToolStore(operations=operations).execute(request)
        replay = await PostgresAssistantToolStore(operations=operations).execute(
            request
        )

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.result == first.result
        assert generated.calls == [request]
        assert database.open_transactions == 0
        assert len(outbox.drafts) == 1
        draft = outbox.drafts[0]
        expected_key = f"assistant-tool:{turn_id}:step:3:call:0:artifact:0"
        assert draft.message_id == OutboundMessageId.for_conversation(
            conversation_id,
            expected_key,
        )
        assert draft.kind == SEND_TELEGRAM_ARTIFACT
        assert draft.payload == {
            "chat_id": 42,
            "artifact_id": "artifact-42",
            "kind": "image",
            "filename": "answer.png",
            "mime_type": "image/png",
            "size_bytes": 321,
        }
        assert draft.idempotency_key == expected_key
        assert outbox.connections == [database.succeeded_connection]

    asyncio.run(scenario())
