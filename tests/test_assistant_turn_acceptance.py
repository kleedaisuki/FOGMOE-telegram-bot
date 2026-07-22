"""@brief 无计费 Assistant acceptance PostgreSQL UoW 测试 / Tests for the no-charge Assistant-acceptance PostgreSQL UoW."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
)
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.infrastructure.database import assistant_turn_acceptance
from fogmoe_bot.infrastructure.database.assistant_turn_acceptance import (
    PostgresAssistantTurnAcceptanceUoW,
)
from fogmoe_bot.infrastructure.database.repositories.user_repository import (
    UserIdentityContext,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 acceptance 时刻 / Fixed acceptance instant."""


class RecordingTransaction:
    """@brief 记录事务退出状态的异步上下文 / Async context recording transaction exit state."""

    def __init__(self) -> None:
        """@brief 初始化伪连接 / Initialize the fake connection."""

        self.connection = object()
        """@brief 调用方可识别的伪连接 / Caller-identifiable fake connection."""
        self.exit_exception: type[BaseException] | None = None
        """@brief 退出时的异常类型 / Exception type at exit."""

    async def __aenter__(self) -> object:
        """@brief 返回同一伪连接 / Return the same fake connection.

        @return 伪连接 / Fake connection.
        """

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 记录退出异常 / Record the exit exception.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常实例 / Exception instance.
        @param traceback 异常回溯 / Exception traceback.
        @return False，以传播异常 / False to propagate an exception.
        """

        del exc, traceback
        self.exit_exception = exc_type
        return False


class FakeWorkflowRepository:
    """@brief 记录同事务 Turn acceptance 调用的替身 / Double recording same-transaction Turn acceptance calls."""

    def __init__(
        self,
        *,
        message_inserted: bool = True,
        activity_inserted: bool = True,
        error: Exception | None = None,
    ) -> None:
        """@brief 初始化效果回执与可选错误 / Initialize effect receipts and an optional error.

        @param message_inserted 用户消息是否新建 / Whether the user message is newly inserted.
        @param activity_inserted 推理活动是否新建 / Whether the inference activity is newly inserted.
        @param error 可选注入错误 / Optional injected error.
        """

        self.message_inserted = message_inserted
        """@brief 用户消息插入标志 / User-message insertion flag."""
        self.activity_inserted = activity_inserted
        """@brief 推理活动插入标志 / Inference-activity insertion flag."""
        self.error = error
        """@brief 注入错误 / Injected error."""
        self.calls: list[tuple[object, object, object, object]] = []
        """@brief 记录 connection、turn、message 与 activity / Recorded connection, turn, message, and activity."""

    async def create_and_accept_turn_in_transaction(
        self,
        connection: object,
        turn: object,
        *,
        message: object,
        activity: object,
        accepted_at: datetime,
    ) -> Any:
        """@brief 记录调用并返回最小化 acceptance 回执 / Record a call and return a minimal acceptance receipt.

        @param connection 当前伪事务连接 / Current fake transaction connection.
        @param turn 待接受 Turn / Turn to accept.
        @param message 待追加消息 / Message to append.
        @param activity 待入队活动 / Activity to enqueue.
        @param accepted_at 接受时刻 / Acceptance instant.
        @return 含 inserted 标志的最小回执 / Minimal receipt containing inserted flags.
        """

        del accepted_at
        self.calls.append((connection, turn, message, activity))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            user_message=SimpleNamespace(inserted=self.message_inserted),
            inference_activity=SimpleNamespace(inserted=self.activity_inserted),
        )


class NullProfileReader:
    """@brief 返回空 Profile 的同事务读取替身 / Transactional profile-reader double returning no profile."""

    def __init__(self) -> None:
        """@brief 初始化调用日志 / Initialize the call log."""

        self.calls: list[tuple[int, object]] = []
        """@brief Profile 读取调用 / Profile-read calls."""

    async def read_profile_in_transaction(
        self,
        user_id: int,
        *,
        connection: object,
    ) -> None:
        """@brief 记录读取并返回空 Profile / Record a read and return no profile.

        @param user_id 用户 ID / User identity.
        @param connection 事务连接 / Transaction connection.
        @return None / None.
        """

        self.calls.append((user_id, connection))
        return None


class FixedPlanResolver:
    """@brief 返回固定封闭方案的同事务替身 / Transaction-bound double returning a fixed closed plan."""

    def __init__(self, plan: AccountPlan = AccountPlan.PAID) -> None:
        """@brief 初始化方案与调用记录 / Initialize the plan and call log.

        @param plan 每次返回的账户方案 / Account plan returned for every call.
        @return None / None.
        """

        self.plan = plan
        """@brief 固定账户方案 / Fixed account plan."""
        self.calls: list[tuple[int, object]] = []
        """@brief user/connection 调用记录 / User/connection call records."""

    async def resolve(self, user_id: int, *, connection: object) -> AccountPlan:
        """@brief 记录调用并返回固定方案 / Record the call and return the fixed plan.

        @param user_id 待分类用户 / User to classify.
        @param connection 当前伪事务 / Current fake transaction.
        @return 固定方案 / Fixed plan.
        """

        self.calls.append((user_id, connection))
        return self.plan


def _request(*, update_id: int = 100) -> AssistantTurnRequest:
    """@brief 构造私聊 Assistant 请求 / Build a private Assistant request.

    @param update_id Telegram Update ID / Telegram Update ID.
    @return 已验证请求 / Validated request.
    """

    return AssistantTurnRequest(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        received_at=NOW,
        user_id=42,
        username="klee",
        display_name="Klee",
        chat_id=42,
        is_group=False,
        message_id=7 + update_id,
        message_thread_id=None,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        user_content={"text": "hello"},
    )


def _group_request() -> AssistantTurnRequest:
    """@brief 构造群 Topic Assistant 请求 / Build a group-topic Assistant request.

    @return 已验证群请求 / Validated group request.
    """

    return AssistantTurnRequest(
        update_id=UpdateId(101),
        conversation_id=ConversationId("assistant-group:-1001:thread:23"),
        received_at=NOW,
        user_id=42,
        username="klee",
        display_name="Klee",
        chat_id=-1001,
        is_group=True,
        message_id=108,
        message_thread_id=23,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-1001:thread:23"),
        user_content={"text": "hello group"},
    )


def _identity_context() -> UserIdentityContext:
    """@brief 构造无货币用户身份上下文 / Build a non-monetary user identity context.

    @return 身份上下文 / Identity context.
    """

    return UserIdentityContext(
        user_id=42,
        permission=1,
        info="CS student",
    )


def test_assistant_turn_request_has_no_coin_price_compatibility_field() -> None:
    """@brief 直接入口不再承载历史金币价格 / Direct ingress carries no legacy coin-price field."""

    request = _request()
    assert not hasattr(request, "coin_cost")
    assert "coin_cost" not in request.user_content


def test_direct_acceptance_reads_identity_only_and_never_touches_balances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 私聊 acceptance 只读取身份上下文，不触及余额 / Private acceptance reads identity only and does not touch balances."""

    async def scenario() -> None:
        """@brief 执行直接 acceptance 场景 / Execute the direct acceptance scenario.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        profiles = NullProfileReader()
        plans = FixedPlanResolver()
        sql_calls: list[str] = []
        identity_calls: list[tuple[int, object, bool]] = []

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 模拟 inbox、Turn 锁与 advisory 锁 / Simulate inbox, Turn, and advisory locks.

            @return 需要时的 inbox 行 / Inbox row when needed.
            """

            del params, connection
            sql_calls.append(sql)
            if "inbound_updates" in sql:
                return ("assistant-user:42",)
            return None

        async def fake_identity(
            user_id: int,
            *,
            connection: object,
            for_update: bool,
        ) -> UserIdentityContext:
            """@brief 返回加锁身份上下文 / Return a locked identity context.

            @return 身份上下文 / Identity context.
            """

            identity_calls.append((user_id, connection, for_update))
            return _identity_context()

        async def fake_diary(user_id: int, *, connection: object) -> bool:
            """@brief 返回日记存在标志 / Return the diary-existence flag.

            @return True / True.
            """

            del user_id, connection
            return True

        async def forbidden_balance_access(*args: object, **kwargs: object) -> None:
            """@brief 证明 acceptance 不可到达余额 API / Prove acceptance cannot reach a balance API.

            @return 永不返回 / Never returns.
            """

            del args, kwargs
            raise AssertionError("zero-cost acceptance touched a balance API")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "transaction", lambda: transaction
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "fetch_one", fake_fetch_one
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_identity_context",
            fake_identity,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            forbidden_balance_access,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(
            workflow,  # type: ignore[arg-type]
            plans=plans,  # type: ignore[arg-type]
            profiles=profiles,  # type: ignore[arg-type]
        ).accept(_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert not result.replayed
        assert identity_calls == [(42, transaction.connection, True)]
        assert plans.calls == [(42, transaction.connection)]
        assert profiles.calls == [(42, transaction.connection)]
        assert all("coins" not in sql.casefold() for sql in sql_calls)
        assert workflow.calls[0][0] is transaction.connection
        activity = workflow.calls[0][3]
        request = activity.request  # type: ignore[attr-defined]
        assert request["user"]["coins"] == 0
        assert request["user"]["plan"] == "paid"
        assert request["user"]["personal_info"] == "CS student"
        assert request["user"]["diary_exists"] is True
        assert transaction.exit_exception is None

    asyncio.run(scenario())


def test_group_acceptance_does_not_read_private_profile_or_diary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 群路径不读取私有状态或余额 / Group path reads neither private state nor balances."""

    async def scenario() -> None:
        """@brief 执行群场景 / Execute the group scenario.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        profiles = NullProfileReader()
        plans = FixedPlanResolver()

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回群 inbox 行 / Return the group inbox row.

            @return inbox 行或 None / Inbox row or None.
            """

            del params, connection
            return (
                ("assistant-group:-1001:thread:23",)
                if "inbound_updates" in sql
                else None
            )

        async def fake_identity(*args: object, **kwargs: object) -> UserIdentityContext:
            """@brief 返回身份上下文 / Return identity context.

            @return 身份上下文 / Identity context.
            """

            del args, kwargs
            return _identity_context()

        async def forbidden_private_read(*args: object, **kwargs: object) -> None:
            """@brief 证明群路径不读取私有状态 / Prove the group path does not read private state.

            @return 永不返回 / Never returns.
            """

            del args, kwargs
            raise AssertionError("group acceptance read private state")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "transaction", lambda: transaction
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "fetch_one", fake_fetch_one
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_identity_context",
            fake_identity,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            forbidden_private_read,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(
            workflow,  # type: ignore[arg-type]
            plans=plans,  # type: ignore[arg-type]
            profiles=profiles,  # type: ignore[arg-type]
        ).accept(_group_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert profiles.calls == []
        request = workflow.calls[0][3].request  # type: ignore[attr-defined]
        assert request["user"]["coins"] == 0
        assert request["user"]["profile"] is None
        assert request["user"]["personal_info"] == ""
        assert request["user"]["diary_exists"] is False

    asyncio.run(scenario())


def test_replay_returns_before_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief durable replay 在身份或余额路径之前收敛 / Durable replay converges before identity or balance paths."""

    async def scenario() -> None:
        """@brief 执行 replay 场景 / Execute the replay scenario.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        turn_id = str(TurnId.for_source(TurnSource.telegram(UpdateId(100))))

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 返回 inbox 与已接受 Turn / Return inbox and an accepted Turn.

            @return 规范查询行 / Canonical query row.
            """

            del params, connection
            if "inbound_updates" in sql:
                return ("assistant-user:42",)
            if "conversation_turns" in sql:
                return (turn_id, "assistant-user:42", "waiting_inference")
            return ()

        async def forbidden_identity(*args: object, **kwargs: object) -> None:
            """@brief 证明 replay 不读用户身份 / Prove replay does not read user identity.

            @return 永不返回 / Never returns.
            """

            del args, kwargs
            raise AssertionError("replay reached identity lookup")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "transaction", lambda: transaction
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "fetch_one", fake_fetch_one
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_identity_context",
            forbidden_identity,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(
            workflow,  # type: ignore[arg-type]
            plans=FixedPlanResolver(),  # type: ignore[arg-type]
        ).accept(_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert result.replayed
        assert workflow.calls == []

    asyncio.run(scenario())


def test_missing_identity_is_a_business_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 未注册身份不创建 Turn / An unregistered identity creates no Turn."""

    async def scenario() -> None:
        """@brief 执行未注册场景 / Execute the unregistered scenario.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox 行 / Return the inbox row.

            @return inbox 行或 None / Inbox row or None.
            """

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def missing_identity(*args: object, **kwargs: object) -> None:
            """@brief 表示身份不存在 / Represent a missing identity.

            @return None / None.
            """

            del args, kwargs
            return None

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "transaction", lambda: transaction
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "fetch_one", fake_fetch_one
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_identity_context",
            missing_identity,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(
            workflow,  # type: ignore[arg-type]
            plans=FixedPlanResolver(),  # type: ignore[arg-type]
        ).accept(_request(), accepted_at=NOW)

        assert isinstance(result, AssistantUserNotRegistered)
        assert workflow.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message_inserted", "activity_inserted"), ((True, False), (False, True))
)
def test_partial_acceptance_receipt_still_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    message_inserted: bool,
    activity_inserted: bool,
) -> None:
    """@brief 半成品 acceptance 仍是事务不变量冲突 / A partial acceptance is still a transaction-invariant conflict."""

    async def scenario() -> None:
        """@brief 执行半成品场景 / Execute the partial-effect scenario.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository(
            message_inserted=message_inserted,
            activity_inserted=activity_inserted,
        )

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox 行 / Return the inbox row.

            @return inbox 行或 None / Inbox row or None.
            """

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_identity(*args: object, **kwargs: object) -> UserIdentityContext:
            """@brief 返回身份上下文 / Return identity context.

            @return 身份上下文 / Identity context.
            """

            del args, kwargs
            return _identity_context()

        async def fake_diary(*args: object, **kwargs: object) -> bool:
            """@brief 返回无日记 / Return no diary.

            @return False / False.
            """

            del args, kwargs
            return False

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "transaction", lambda: transaction
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection, "fetch_one", fake_fetch_one
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_identity_context",
            fake_identity,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )

        with pytest.raises(IdempotencyConflictError, match="partial durable effect"):
            await PostgresAssistantTurnAcceptanceUoW(
                workflow,  # type: ignore[arg-type]
                plans=FixedPlanResolver(),  # type: ignore[arg-type]
            ).accept(_request(), accepted_at=NOW)
        assert transaction.exit_exception is IdempotencyConflictError

    asyncio.run(scenario())
