"""@brief 银行申请与 transactional outbox 原子性测试 / Tests for atomic bank-request and transactional-outbox behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.banking.models import (
    RequestTokens,
    ReviewTokenRequest,
    TokenReviewDecision,
)
from fogmoe_bot.domain.banking.money import TokenAmount, TokenBucket
from fogmoe_bot.domain.banking.requests import TokenRequest, TokenRequestStatus
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.outbox import OutboundDraft, OutboundEnqueueResult
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.bank_notifications import (
    BankTokenRequestNotificationWriter,
    PostgresBankTokenRequestNotificationWriter,
)
from fogmoe_bot.infrastructure.database.banking import PostgresBankOperations


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定的有时区测试时刻 / Fixed timezone-aware test instant."""


class _DeduplicatingOutbox:
    """@brief 模拟 standalone outbox 的语义去重 / Test double simulating standalone-outbox semantic deduplication."""

    def __init__(self) -> None:
        """@brief 初始化规范 draft 映射 / Initialize the canonical draft map.

        @return None / None.
        """

        self.drafts: dict[tuple[ConversationId, str], OutboundDraft] = {}
        """@brief 按 conversation/idempotency 归并的规范草稿 / Canonical drafts keyed by conversation/idempotency."""
        self.connections: list[AsyncConnection] = []
        """@brief 调用方提供的事务连接 / Caller-provided transactional connections."""

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 记录或验证确定性 outbox 草稿 / Record or verify a deterministic outbox draft.

        @param connection 调用方事务连接 / Caller transaction connection.
        @param draft 待写入草稿 / Draft to persist.
        @return 未被本测试读取的占位入队结果 / Placeholder enqueue result unused by this test.
        """

        self.connections.append(connection)
        key = (draft.conversation_id, draft.idempotency_key)
        existing = self.drafts.get(key)
        if existing is not None:
            assert existing.message_id == draft.message_id
            assert existing.conversation_id == draft.conversation_id
            assert existing.turn_id == draft.turn_id
            assert existing.delivery_stream_id == draft.delivery_stream_id
            assert existing.kind == draft.kind
            assert existing.payload == draft.payload
            assert existing.idempotency_key == draft.idempotency_key
        else:
            self.drafts[key] = draft
        return cast(OutboundEnqueueResult, None)


class _RecordingNotifications:
    """@brief 记录银行操作传入的同事务通知调用 / Record same-transaction notification calls made by banking operations."""

    def __init__(self, *, fail: bool = False) -> None:
        """@brief 初始化通知调用记录 / Initialize notification-call recordings.

        @param fail 是否在写入通知时模拟故障 / Whether to simulate a notification-write failure.
        @return None / None.
        """

        self.created: list[tuple[TokenRequest, AsyncConnection]] = []
        """@brief 待审核申请提醒调用 / Pending-request notification calls."""
        self.reviewed: list[tuple[TokenRequest, AsyncConnection]] = []
        """@brief 审核终态回执调用 / Review-terminal notification calls."""
        self._fail = fail
        """@brief 故障注入开关 / Failure-injection switch."""

    async def enqueue_request_created(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 记录管理员提醒调用 / Record an administrator-reminder invocation.

        @param request 待审核申请 / Pending request.
        @param connection 调用方事务连接 / Caller transaction connection.
        @return None / None.
        """

        self.created.append((request, connection))
        if self._fail:
            raise RuntimeError("simulated notification persistence failure")

    async def enqueue_request_reviewed(
        self,
        request: TokenRequest,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 记录申请人回执调用 / Record a requester-receipt invocation.

        @param request 审核终态申请 / Terminal request.
        @param connection 调用方事务连接 / Caller transaction connection.
        @return None / None.
        """

        self.reviewed.append((request, connection))
        if self._fail:
            raise RuntimeError("simulated notification persistence failure")


def _pending_request() -> TokenRequest:
    """@brief 构造待审核申请 / Build a pending token request.

    @return 待审核申请 / Pending token request.
    """

    return TokenRequest(
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        requester_id=42,
        requested_amount=TokenAmount(12),
        requested_bucket=TokenBucket.FREE,
        purpose="修复个人灯塔",
        status=TokenRequestStatus.PENDING,
        requested_at=NOW,
    )


def _approved_request() -> TokenRequest:
    """@brief 构造已批准申请 / Build an approved token request.

    @return 已批准申请 / Approved token request.
    """

    pending = _pending_request()
    return pending.approve(
        reviewer_id=1,
        reviewed_at=NOW,
        ledger_entry_id=UUID("22222222-2222-4222-8222-222222222222"),
        note="已核对活动记录",
    )


def test_notification_writer_creates_deterministic_admin_reminder() -> None:
    """@brief 同一待审核申请重放只定义一条管理员提醒 / One pending-request replay defines one administrator reminder.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行确定性草稿场景 / Execute the deterministic-draft scenario.

        @return None / None.
        """

        outbox = _DeduplicatingOutbox()
        writer = PostgresBankTokenRequestNotificationWriter(
            outbox=outbox,
            administrator_id=1,
        )
        connection = cast(AsyncConnection, object())
        request = _pending_request()

        await writer.enqueue_request_created(request, connection=connection)
        await writer.enqueue_request_created(request, connection=connection)

        assert len(outbox.drafts) == 1
        draft = next(iter(outbox.drafts.values()))
        assert draft.conversation_id == ConversationId(
            f"bank-token-request:{request.request_id}"
        )
        assert draft.delivery_stream_id.value == "telegram:primary:chat:1:thread:0"
        assert draft.idempotency_key == (
            f"request:{request.request_id}:administrator-review-notification"
        )
        assert draft.payload["chat_id"] == 1
        text = str(draft.payload["text"])
        assert f"申请 ID：{request.request_id}" in text
        assert "申请人：42" in text
        assert "数量：12 Free" in text
        assert "用途：修复个人灯塔" in text
        assert f"/bank_review {request.request_id} approve" in text
        assert f"/bank_review {request.request_id} reject" in text

    asyncio.run(scenario())


def test_notification_writer_creates_deterministic_requester_receipt() -> None:
    """@brief 审批终态为申请人定义一条确定性回执 / A review terminal state defines one deterministic requester receipt.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行终态回执场景 / Execute the terminal-receipt scenario.

        @return None / None.
        """

        outbox = _DeduplicatingOutbox()
        writer = PostgresBankTokenRequestNotificationWriter(
            outbox=outbox,
            administrator_id=1,
        )
        connection = cast(AsyncConnection, object())
        request = _approved_request()

        await writer.enqueue_request_reviewed(request, connection=connection)
        await writer.enqueue_request_reviewed(request, connection=connection)

        assert len(outbox.drafts) == 1
        draft = next(iter(outbox.drafts.values()))
        assert draft.delivery_stream_id.value == "telegram:primary:chat:42:thread:0"
        assert draft.idempotency_key == (
            f"decision:{request.request_id}:v1:requester-review-notification"
        )
        assert draft.payload["chat_id"] == 42
        assert "已批准" in str(draft.payload["text"])
        assert str(request.ledger_entry_id) in str(draft.payload["text"])

    asyncio.run(scenario())


def test_request_operation_commits_state_and_notification_as_one_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 申请状态与管理员提醒共享一个事务连接 / Request state and administrator reminder share one transaction connection.

    @param monkeypatch pytest monkeypatch fixture / Pytest monkeypatch fixture.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行成功与失败事务场景 / Execute successful and failed transaction scenarios.

        @return None / None.
        """

        events: list[str] = []
        connection = cast(AsyncConnection, object())

        @asynccontextmanager
        async def transaction() -> AsyncIterator[AsyncConnection]:
            """@brief 模拟短事务并记录提交或回滚 / Simulate a short transaction and record commit or rollback.

            @return 活动连接 iterator / Active connection iterator.
            """

            events.append("enter")
            try:
                yield connection
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        async def fetch_one(
            sql: str,
            params: Iterable[object] | Mapping[str, object] | None = None,
            *,
            mapping: bool = False,
            connection: AsyncConnection | None = None,
        ) -> tuple[object, ...] | None:
            """@brief 按 SQL 意图返回最小化测试行 / Return minimal test rows by SQL intent.

            @param sql 参数化 SQL / Parameterized SQL.
            @param params SQL 参数 / SQL parameters.
            @param mapping 是否请求映射行 / Whether a mapping row was requested.
            @param connection 当前事务连接 / Current transaction connection.
            @return 最小化行或 None / Minimal row or None.
            """

            del params, mapping
            assert connection is not None
            if "pg_advisory_xact_lock" in sql:
                return (None,)
            if "FROM identity.users" in sql:
                return (1,)
            if "FROM bank.operation_receipts" in sql:
                return None
            raise AssertionError(f"unexpected fetch_one SQL: {sql}")

        async def execute(
            sql: str,
            params: Iterable[object] | Mapping[str, object] | None = None,
            *,
            connection: AsyncConnection | None = None,
        ) -> int:
            """@brief 记录银行持久化写入 / Record bank persistence writes.

            @param sql 参数化 SQL / Parameterized SQL.
            @param params SQL 参数 / SQL parameters.
            @param connection 当前事务连接 / Current transaction connection.
            @return 一行受影响 / One affected row.
            """

            del params
            assert connection is not None
            events.append("write")
            assert "bank.token_requests" in sql or "bank.operation_receipts" in sql
            return 1

        monkeypatch.setattr(db_connection, "transaction", transaction)
        monkeypatch.setattr(db_connection, "fetch_one", fetch_one)
        monkeypatch.setattr(db_connection, "execute", execute)

        notifications = _RecordingNotifications()
        operations = PostgresBankOperations(
            notifications=cast(BankTokenRequestNotificationWriter, notifications),
        )
        result = await operations.request_tokens(
            RequestTokens(
                user_id=42,
                amount=TokenAmount(7),
                purpose="重建港口灯塔",
                requested_at=NOW,
                idempotency_key="test:atomic-request",
                request_id=UUID("33333333-3333-4333-8333-333333333333"),
            )
        )

        assert result.request is not None
        assert notifications.created == [(result.request, connection)]
        assert events[-1] == "commit"

        failing_notifications = _RecordingNotifications(fail=True)
        failing_operations = PostgresBankOperations(
            notifications=cast(BankTokenRequestNotificationWriter, failing_notifications),
        )
        with pytest.raises(RuntimeError, match="notification persistence failure"):
            await failing_operations.request_tokens(
                RequestTokens(
                    user_id=42,
                    amount=TokenAmount(8),
                    purpose="重建港口灯塔的备用供电",
                    requested_at=NOW,
                    idempotency_key="test:atomic-request-failure",
                    request_id=UUID("44444444-4444-4444-8444-444444444444"),
                )
            )
        assert events[-1] == "rollback"

    asyncio.run(scenario())


def test_review_operation_writes_requester_receipt_in_its_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 审核终态与申请人回执共享一个事务连接 / Review terminal state and requester receipt share one transaction connection.

    @param monkeypatch pytest monkeypatch fixture / Pytest monkeypatch fixture.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行拒绝申请审核场景 / Execute the reject-review scenario.

        @return None / None.
        """

        connection = cast(AsyncConnection, object())
        request = _pending_request()

        @asynccontextmanager
        async def transaction() -> AsyncIterator[AsyncConnection]:
            """@brief 提供一个活动事务连接 / Provide one active transaction connection.

            @return 活动连接 iterator / Active connection iterator.
            """

            yield connection

        async def fetch_one(
            sql: str,
            params: Iterable[object] | Mapping[str, object] | None = None,
            *,
            mapping: bool = False,
            connection: AsyncConnection | None = None,
        ) -> tuple[object, ...] | None:
            """@brief 返回锁定的 pending request / Return the locked pending request.

            @param sql 参数化 SQL / Parameterized SQL.
            @param params SQL 参数 / SQL parameters.
            @param mapping 是否请求映射行 / Whether a mapping row was requested.
            @param connection 当前事务连接 / Current transaction connection.
            @return 事务需要的测试行 / Test row required by the transaction.
            """

            del params, mapping
            assert connection is not None
            if "pg_advisory_xact_lock" in sql:
                return (None,)
            if "FROM bank.operation_receipts" in sql:
                return None
            if "FROM bank.token_requests" in sql:
                return (
                    request.request_id,
                    request.requester_id,
                    request.requested_amount.value,
                    request.requested_bucket.value,
                    request.purpose,
                    request.status.value,
                    request.requested_at,
                    None,
                    None,
                    None,
                    None,
                    request.version,
                )
            raise AssertionError(f"unexpected fetch_one SQL: {sql}")

        async def execute(
            sql: str,
            params: Iterable[object] | Mapping[str, object] | None = None,
            *,
            connection: AsyncConnection | None = None,
        ) -> int:
            """@brief 接受审核状态与回执写入 / Accept review-state and receipt writes.

            @param sql 参数化 SQL / Parameterized SQL.
            @param params SQL 参数 / SQL parameters.
            @param connection 当前事务连接 / Current transaction connection.
            @return 一行受影响 / One affected row.
            """

            del sql, params
            assert connection is not None
            return 1

        monkeypatch.setattr(db_connection, "transaction", transaction)
        monkeypatch.setattr(db_connection, "fetch_one", fetch_one)
        monkeypatch.setattr(db_connection, "execute", execute)

        notifications = _RecordingNotifications()
        operations = PostgresBankOperations(
            notifications=cast(BankTokenRequestNotificationWriter, notifications),
        )
        result = await operations.review_token_request(
            ReviewTokenRequest(
                request_id=request.request_id,
                reviewer_id=1,
                decision=TokenReviewDecision.REJECT,
                reviewed_at=NOW,
                idempotency_key="test:atomic-review",
                note="缺少可核验活动记录",
            )
        )

        assert result.request is not None
        assert result.request.status is TokenRequestStatus.REJECTED
        assert notifications.reviewed == [(result.request, connection)]

    asyncio.run(scenario())
