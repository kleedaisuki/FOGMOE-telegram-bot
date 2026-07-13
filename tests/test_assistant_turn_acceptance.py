"""@brief PostgreSQL Assistant acceptance UoW 测试 / Tests for the PostgreSQL Assistant acceptance UoW."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any

import pytest

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantInsufficientCoins,
    AssistantTurnAccepted,
    AssistantTurnRequest,
)
from fogmoe_bot.application.conversation.workflow import PreparedTurnAcceptance
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    MessageSequence,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.turn import TurnEvent
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityEnqueueResult,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageAppendResult,
)
from fogmoe_bot.domain.conversation.workflow_results import TurnAcceptanceResult
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.economy import AssistantBillingReservation
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    UserProfileSnapshot,
)
from fogmoe_bot.infrastructure.database import assistant_turn_acceptance
from fogmoe_bot.infrastructure.database.assistant_turn_acceptance import (
    PostgresAssistantTurnAcceptanceUoW,
)
from fogmoe_bot.infrastructure.database.repositories.user_repository import UserAccount


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""

ADMINISTRATOR_ID = 1002288404
"""@brief 测试管理员 Telegram 用户 ID / Test administrator Telegram user ID."""


class RecordingTransaction:
    """@brief 记录事务连接与异常 / Record transaction connection and exception."""

    def __init__(self, lock: asyncio.Lock | None = None) -> None:
        """@brief 初始化事务 / Initialize the transaction.

        @param lock 可选串行锁 / Optional serialization lock.
        """

        self.connection = object()
        """@brief 模拟连接 / Fake connection."""
        self.lock = lock
        """@brief 模拟数据库行锁 / Simulated database row lock."""
        self.exit_exception: type[BaseException] | None = None
        """@brief 退出异常 / Exit exception."""

    async def __aenter__(self) -> object:
        """@brief 进入事务并可选取得锁 / Enter and optionally acquire the lock.

        @return 模拟连接 / Fake connection.
        """

        if self.lock is not None:
            await self.lock.acquire()
        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 记录退出并释放锁 / Record exit and release the lock.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常 / Exception.
        @param traceback 回溯 / Traceback.
        @return False，传播异常 / False to propagate errors.
        """

        del exc, traceback
        self.exit_exception = exc_type
        if self.lock is not None:
            self.lock.release()
        return False


class FakeWorkflowRepository:
    """@brief connection-bound workflow primitive 替身 / Connection-bound workflow primitive double."""

    def __init__(
        self,
        error: Exception | None = None,
        *,
        message_inserted: bool = True,
        activity_inserted: bool = True,
    ) -> None:
        """@brief 初始化替身 / Initialize the double.

        @param error 可选注入错误 / Optional injected error.
        @param message_inserted 用户消息是否新建 / Whether the user message was inserted.
        @param activity_inserted 推理活动是否新建 / Whether the inference activity was inserted.
        """

        self.error = error
        """@brief 注入错误 / Injected error."""
        self.message_inserted = message_inserted
        """@brief 用户消息写入结果 / User-message insert result."""
        self.activity_inserted = activity_inserted
        """@brief 推理活动写入结果 / Inference-activity insert result."""
        self.calls: list[tuple[object, PreparedTurnAcceptance]] = []
        """@brief primitive 调用 / Primitive calls."""

    async def create_and_accept_turn_in_transaction(
        self,
        connection: object,
        turn: object,
        *,
        message: object,
        activity: object,
        accepted_at: datetime,
    ) -> TurnAcceptanceResult:
        """@brief 记录同连接调用并构造 acceptance / Record the same-connection call and build an acceptance.

        @return acceptance result / Acceptance result.
        """

        prepared = PreparedTurnAcceptance(
            turn=turn,  # type: ignore[arg-type]
            message=message,  # type: ignore[arg-type]
            activity=activity,  # type: ignore[arg-type]
            accepted_at=accepted_at,
        )
        self.calls.append((connection, prepared))
        if self.error is not None:
            raise self.error
        accepted_turn = prepared.turn.transition(
            TurnEvent.ACCEPT,
            occurred_at=accepted_at,
        ).transition(
            TurnEvent.REQUEST_INFERENCE,
            occurred_at=accepted_at,
        )
        return TurnAcceptanceResult(
            turn=accepted_turn,
            user_message=MessageAppendResult(
                message=ConversationMessage(
                    draft=prepared.message,
                    sequence=MessageSequence(1),
                ),
                inserted=self.message_inserted,
            ),
            inference_activity=InferenceActivityEnqueueResult(
                activity=InferenceActivity.pending(prepared.activity),
                inserted=self.activity_inserted,
            ),
        )


class RecordingBilling:
    """@brief 记录 acceptance 计费交互的替身 / Double recording acceptance billing interactions."""

    def __init__(self) -> None:
        """@brief 初始化空调用日志 / Initialize empty call logs."""

        self.reservations: list[tuple[object, AssistantBillingReservation]] = []
        """@brief 新预留调用 / New-reservation calls."""
        self.validations: list[tuple[object, TurnId, int, int, Decimal | None]] = []
        """@brief replay identity 校验调用 / Replay-identity validation calls."""

    async def reserve(
        self,
        connection: object,
        reservation: AssistantBillingReservation,
    ) -> bool:
        """@brief 记录预留 / Record a reservation.

        @param connection 调用方事务 / Caller transaction.
        @param reservation 计费事实 / Billing fact.
        @return 始终表示首次插入 / Always report a first insertion.
        """

        self.reservations.append((connection, reservation))
        return True

    async def validate_expected(
        self,
        connection: object,
        *,
        turn_id: TurnId,
        user_id: int,
        cost: int,
        pool_contribution: Decimal | None,
    ) -> AssistantBillingReservation | None:
        """@brief 记录 replay identity 校验 / Record replay-identity validation.

        @return 测试无需返回规范行 / Tests do not require a canonical row.
        """

        self.validations.append((connection, turn_id, user_id, cost, pool_contribution))
        return None

    async def settle(self, *args: object, **kwargs: object) -> None:
        """@brief acceptance 不得结算 / Acceptance must not settle."""

        del args, kwargs
        raise AssertionError("acceptance attempted to settle billing")

    async def release(self, *args: object, **kwargs: object) -> None:
        """@brief acceptance 不得释放 / Acceptance must not release."""

        del args, kwargs
        raise AssertionError("acceptance attempted to release billing")


class FrozenProfileReader:
    """@brief 记录 acceptance transaction 内唯一 Profile 读取 / Record the sole Profile read inside acceptance."""

    def __init__(self, snapshot: UserProfileSnapshot | None) -> None:
        """@brief 保存返回快照 / Store the returned snapshot."""

        self.snapshot = snapshot
        self.calls: list[tuple[int, object]] = []

    async def read_profile_in_transaction(
        self,
        user_id: int,
        *,
        connection: object,
    ) -> UserProfileSnapshot | None:
        """@brief 返回同一 committed snapshot / Return the same committed snapshot."""

        self.calls.append((user_id, connection))
        return self.snapshot


def _profile_snapshot() -> UserProfileSnapshot:
    """@brief 构造 acceptance 可冻结的 Profile / Build a Profile pinnable at acceptance."""

    return UserProfileSnapshot(
        user_id=42,
        revision=3,
        document=ProfileDocument(
            (
                ProfileClaim(
                    key="drink.preference",
                    kind=ProfileClaimKind.PREFERENCE,
                    statement="偏好茶",
                    confidence=ProfileConfidence.EXPLICIT,
                    evidence_event_ids=(7,),
                    observed_at=NOW,
                ),
            )
        ),
        observed_through_event_id=7,
        created_at=NOW,
        updated_at=NOW,
        route_key="test:profile-model",
        prompt_version=1,
    )


def _request(*, update_id: int = 100, cost: int = 4) -> AssistantTurnRequest:
    """@brief 构造预检 Assistant 请求 / Build a preflighted Assistant request.

    @param update_id Update ID / Update ID.
    @param cost 费用 / Charge.
    @return Assistant 请求 / Assistant request.
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
        coin_cost=cost,
    )


def _group_request(*, update_id: int = 101, cost: int = 0) -> AssistantTurnRequest:
    """@brief 构造共享群 Topic 请求 / Build a shared group-topic request.

    @param update_id Update ID / Update ID.
    @param cost 费用 / Charge.
    @return 群聊 Assistant 请求 / Group Assistant request.
    """

    return AssistantTurnRequest(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-group:-1001:thread:23"),
        received_at=NOW,
        user_id=42,
        username="klee",
        display_name="Klee",
        chat_id=-1001,
        is_group=True,
        message_id=7 + update_id,
        message_thread_id=23,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-1001:thread:23"),
        user_content={"text": "hello group"},
        coin_cost=cost,
    )


def _account(*, free: int = 2, paid: int = 5) -> UserAccount:
    """@brief 构造账户快照 / Build an account snapshot.

    @param free 免费余额 / Free balance.
    @param paid 付费余额 / Paid balance.
    @return 账户 / Account.
    """

    return UserAccount(
        user_id=42,
        permission=1,
        coins=free,
        coins_paid=paid,
        info="CS student",
        name="Klee",
        user_plan="paid",
    )


def test_success_locks_rows_and_commits_charge_pool_and_acceptance_on_one_connection(
    monkeypatch: Any,
) -> None:
    """@brief 成功路径在同一短事务锁行并提交全部变化 / Success locks rows and commits every change on one short transaction."""

    async def scenario() -> None:
        """@brief 执行成功 UoW / Execute a successful UoW.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        sql_calls: list[tuple[str, object]] = []
        account_reads: list[tuple[int, object, bool]] = []
        balance_writes: list[tuple[int, int, int, str, object]] = []
        billing = RecordingBilling()
        profiles = FrozenProfileReader(_profile_snapshot())

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox identity 且声明无既有 Turn / Return the inbox identity and no existing Turn."""

            sql_calls.append((sql, connection))
            if "inbound_updates" in sql:
                return ("assistant-user:42",)
            return None

        async def fake_account(
            user_id: int,
            *,
            connection: object,
            for_update: bool,
        ) -> UserAccount:
            """@brief 返回加锁账户 / Return the locked account."""

            account_reads.append((user_id, connection, for_update))
            return _account()

        async def fake_diary(user_id: int, *, connection: object) -> bool:
            """@brief 返回日记存在 / Return diary existence."""

            del user_id, connection
            return True

        async def fake_balances(
            user_id: int,
            free: int,
            paid: int,
            plan: str,
            *,
            connection: object,
        ) -> None:
            """@brief 记录余额写入 / Record balance writes."""

            balance_writes.append((user_id, free, paid, plan, connection))

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            fake_balances,
        )
        result = await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
            workflow,
            billing=billing,  # type: ignore[arg-type]
            profiles=profiles,  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        ).accept(_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert result.replayed is False
        assert all(
            "FOR UPDATE" in sql
            for sql, _connection in sql_calls
            if "inbound_updates" in sql or "conversation_turns" in sql
        )
        assert any("pg_advisory_xact_lock" in sql for sql, _connection in sql_calls)
        assert account_reads == [(42, transaction.connection, True)]
        assert balance_writes == [(42, 0, 3, "paid", transaction.connection)]
        assert len(billing.reservations) == 1
        billing_connection, reservation = billing.reservations[0]
        assert billing_connection is transaction.connection
        assert reservation.user_id == 42
        assert reservation.cost == 4
        assert reservation.free_reserved == 2
        assert reservation.paid_reserved == 2
        assert reservation.pool_contribution == Decimal("0.80")
        assert billing.validations == []
        assert workflow.calls[0][0] is transaction.connection
        inference = workflow.calls[0][1].activity.request
        assert inference["user"] == {
            "user_id": 42,
            "username": "klee",
            "display_name": "Klee",
            "coins": 3,
            "plan": "paid",
            "permission": 1,
            "profile": {
                "revision": 3,
                "observed_through_event_id": 7,
                "prompt_version": 1,
                "route_key": "test:profile-model",
                "created_at": "2030-01-01T00:00:00Z",
                "updated_at": "2030-01-01T00:00:00Z",
                "claims": [
                    {
                        "key": "drink.preference",
                        "kind": "preference",
                        "statement": "偏好茶",
                        "confidence": "explicit",
                        "evidence_event_ids": [7],
                        "observed_at": "2030-01-01T00:00:00Z",
                    }
                ],
            },
            "personal_info": "CS student",
            "diary_exists": True,
        }
        assert profiles.calls == [(42, transaction.connection)]
        assert transaction.exit_exception is None

    asyncio.run(scenario())


def test_group_acceptance_never_reads_or_freezes_private_user_state(
    monkeypatch: Any,
) -> None:
    """@brief 群 acceptance 不读取或冻结私人 Profile/日记 / Group acceptance neither reads nor freezes private Profile or diary state."""

    async def scenario() -> None:
        """@brief 接受一个零费用群 Topic Turn / Accept one zero-cost group-topic Turn."""

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        billing = RecordingBilling()
        profiles = FrozenProfileReader(_profile_snapshot())

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回群 inbox identity 且无既有 Turn / Return group inbox identity and no existing Turn."""

            del params, connection
            if "inbound_updates" in sql:
                return ("assistant-group:-1001:thread:23",)
            return None

        async def fake_account(
            user_id: int,
            *,
            connection: object,
            for_update: bool,
        ) -> UserAccount:
            """@brief 返回加锁账户 / Return a locked account."""

            del user_id, connection
            assert for_update is True
            return _account()

        async def forbidden_diary(*args: object, **kwargs: object) -> bool:
            """@brief 证明群路径不读取私人日记 / Prove the group path never reads the private diary."""

            del args, kwargs
            raise AssertionError("group acceptance read a private diary")

        async def forbidden_balance_write(*args: object, **kwargs: object) -> None:
            """@brief 零费用路径不得写余额 / Zero-cost path must not write balances."""

            del args, kwargs
            raise AssertionError("zero-cost group acceptance wrote balances")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            forbidden_diary,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            forbidden_balance_write,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
            workflow,
            billing=billing,  # type: ignore[arg-type]
            profiles=profiles,  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        ).accept(_group_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert profiles.calls == []
        inference = workflow.calls[0][1].activity.request
        assert inference["user"] == {
            "user_id": 42,
            "username": "klee",
            "display_name": "Klee",
            "coins": 7,
            "plan": "paid",
            "permission": 1,
            "profile": None,
            "personal_info": "",
            "diary_exists": False,
        }
        assert inference["scope"] == {
            "is_group": True,
            "group_id": -1001,
            "message_id": 108,
            "message_thread_id": 23,
        }

    asyncio.run(scenario())


def test_zero_cost_translation_accepts_without_balance_or_pool_write(
    monkeypatch: Any,
) -> None:
    """@brief 0 费用翻译仍原子接受，但不写余额或奖池 / A zero-cost translation is still atomically accepted without balance or pool writes."""

    async def scenario() -> None:
        """@brief 执行 0 费用 acceptance / Execute zero-cost acceptance."""

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        billing = RecordingBilling()

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox 且无既有 Turn / Return the inbox identity and no existing Turn."""

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_account(
            user_id: int,
            *,
            connection: object,
            for_update: bool,
        ) -> UserAccount:
            """@brief 返回加锁账户 / Return a locked account."""

            del user_id, connection
            assert for_update is True
            return _account()

        async def fake_diary(user_id: int, *, connection: object) -> bool:
            """@brief 返回无日记 / Return no diary."""

            del user_id, connection
            return False

        async def forbidden_write(*args: object, **kwargs: object) -> None:
            """@brief 拒绝任何账户/奖池写入 / Reject every account or pool write."""

            del args, kwargs
            raise AssertionError("zero-cost acceptance attempted a billing write")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            forbidden_write,
        )
        request = _request(cost=0)
        result = await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
            workflow,
            billing=billing,  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        ).accept(request, accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert result.replayed is False
        assert len(workflow.calls) == 1
        inference = workflow.calls[0][1].activity.request
        assert inference["user"]["coins"] == 7  # type: ignore[index]
        assert billing.reservations == []
        assert billing.validations == [
            (
                transaction.connection,
                TurnId.for_source(TurnSource.telegram(UpdateId(100))),
                42,
                0,
                None,
            )
        ]
        assert transaction.exit_exception is None

    asyncio.run(scenario())


def test_duplicate_update_returns_replay_before_account_or_pool_mutation(
    monkeypatch: Any,
) -> None:
    """@brief 重放 Update 在账户锁与扣费前收敛 / A replayed Update converges before account locking or charging."""

    async def scenario() -> None:
        """@brief 执行已有 Turn 的 replay / Replay an Update that already owns a Turn.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository()
        billing = RecordingBilling()
        turn_id = str(TurnId.for_source(TurnSource.telegram(UpdateId(100))))

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 返回 inbox 与已接受 Turn / Return the inbox and accepted Turn."""

            del params, connection
            if "inbound_updates" in sql:
                return ("assistant-user:42",)
            return (turn_id, "assistant-user:42", "waiting_inference")

        async def forbidden_account(*args: object, **kwargs: object) -> None:
            """@brief 账户不应被读取 / Account must not be read."""

            del args, kwargs
            raise AssertionError("duplicate update reached account mutation")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            forbidden_account,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
            workflow,
            billing=billing,  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        ).accept(_request(), accepted_at=NOW)

        assert isinstance(result, AssistantTurnAccepted)
        assert result.replayed is True
        assert workflow.calls == []
        assert billing.reservations == []
        assert billing.validations == [
            (
                transaction.connection,
                TurnId.for_source(TurnSource.telegram(UpdateId(100))),
                42,
                4,
                Decimal("0.80"),
            )
        ]

    asyncio.run(scenario())


def test_insufficient_balance_does_not_create_turn_or_mutate_pool(
    monkeypatch: Any,
) -> None:
    """@brief 余额不足只读加锁账户，不创建 Turn 或修改奖池 / Insufficient balance only reads the locked account and creates neither Turn nor pool mutation."""

    async def scenario() -> None:
        """@brief 执行余额不足 UoW / Execute an insufficient-balance UoW.

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
            """@brief 返回 inbox 且无 Turn / Return the inbox and no Turn."""

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_account(*args: object, **kwargs: object) -> UserAccount:
            """@brief 返回零余额账户 / Return a zero-balance account."""

            del args, kwargs
            return _account(free=0, paid=0)

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )

        result = await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
            workflow,
            RecordingBilling(),  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        ).accept(_request(cost=1), accepted_at=NOW)

        assert isinstance(result, AssistantInsufficientCoins)
        assert workflow.calls == []

    asyncio.run(scenario())


def test_concurrent_requests_for_one_account_cannot_overspend(monkeypatch: Any) -> None:
    """@brief 并发 Update 由账户行锁串行，最多一个扣费成功 / Concurrent Updates serialize on the account row and at most one charge succeeds."""

    async def scenario() -> None:
        """@brief 以一枚余额并发接受两次 / Concurrently accept twice against one coin.

        @return None / None.
        """

        row_lock = asyncio.Lock()
        balance = 1
        workflow = FakeWorkflowRepository()
        billing = RecordingBilling()

        def transaction_factory() -> RecordingTransaction:
            """@brief 创建共享行锁事务 / Create a transaction sharing the row lock.

            @return 事务 / Transaction.
            """

            return RecordingTransaction(row_lock)

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 每个 Update 有 inbox 且尚无 Turn / Every Update has an inbox row and no Turn."""

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_account(*args: object, **kwargs: object) -> UserAccount:
            """@brief 返回锁内最新余额 / Return the latest balance under the row lock."""

            del args, kwargs
            return _account(free=balance, paid=0)

        async def fake_balances(
            user_id: int,
            free: int,
            paid: int,
            plan: str,
            *,
            connection: object,
        ) -> None:
            """@brief 原子更新共享余额 / Atomically update the shared balance."""

            del user_id, paid, plan, connection
            nonlocal balance
            balance = free

        async def fake_diary(*args: object, **kwargs: object) -> bool:
            """@brief 返回无日记 / Return no diary."""

            del args, kwargs
            return False

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            transaction_factory,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            fake_balances,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )
        uow = PostgresAssistantTurnAcceptanceUoW(
            workflow,  # type: ignore[arg-type]
            billing=billing,  # type: ignore[arg-type]
            administrator_id=ADMINISTRATOR_ID,
        )

        results = await asyncio.gather(
            uow.accept(_request(update_id=100, cost=1), accepted_at=NOW),
            uow.accept(_request(update_id=101, cost=1), accepted_at=NOW),
        )

        assert sum(isinstance(result, AssistantTurnAccepted) for result in results) == 1
        assert (
            sum(isinstance(result, AssistantInsufficientCoins) for result in results)
            == 1
        )
        assert balance == 0
        assert len(workflow.calls) == 1
        assert len(billing.reservations) == 1

    asyncio.run(scenario())


def test_workflow_failure_rolls_back_before_balance_or_pool_write(
    monkeypatch: Any,
) -> None:
    """@brief workflow 写入失败传播并由同事务回滚，余额和奖池未写 / Workflow failure propagates for same-transaction rollback before balance or pool writes."""

    async def scenario() -> None:
        """@brief 注入 primitive 失败 / Inject a primitive failure.

        @return None / None.
        """

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository(RuntimeError("activity insert failed"))
        writes: list[str] = []

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox 且无 Turn / Return the inbox and no Turn."""

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_account(*args: object, **kwargs: object) -> UserAccount:
            """@brief 返回足额账户 / Return a funded account."""

            del args, kwargs
            return _account()

        async def fake_diary(*args: object, **kwargs: object) -> bool:
            """@brief 返回无日记 / Return no diary."""

            del args, kwargs
            return False

        async def forbidden_write(*args: object, **kwargs: object) -> None:
            """@brief 记录不应发生的写入 / Record a write that must not occur."""

            del args, kwargs
            writes.append("write")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            forbidden_write,
        )
        with pytest.raises(RuntimeError, match="activity insert failed"):
            await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
                workflow,
                RecordingBilling(),  # type: ignore[arg-type]
                administrator_id=ADMINISTRATOR_ID,
            ).accept(_request(), accepted_at=NOW)

        assert writes == []
        assert transaction.exit_exception is RuntimeError

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message_inserted", "activity_inserted"),
    ((True, False), (False, True)),
)
def test_partial_acceptance_receipt_is_invariant_conflict_and_rolls_back(
    monkeypatch: Any,
    message_inserted: bool,
    activity_inserted: bool,
) -> None:
    """@brief acceptance 半成品不能伪装成 replay / A partial acceptance cannot masquerade as a replay."""

    async def scenario() -> None:
        """@brief 注入 message/activity 异或回执 / Inject an exclusive-or message/activity receipt."""

        transaction = RecordingTransaction()
        workflow = FakeWorkflowRepository(
            message_inserted=message_inserted,
            activity_inserted=activity_inserted,
        )
        writes: list[str] = []

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...] | None:
            """@brief 返回 inbox 且无既存 Turn / Return the inbox and no existing Turn."""

            del params, connection
            return ("assistant-user:42",) if "inbound_updates" in sql else None

        async def fake_account(*args: object, **kwargs: object) -> UserAccount:
            """@brief 返回足额账户 / Return a funded account."""

            del args, kwargs
            return _account()

        async def fake_diary(*args: object, **kwargs: object) -> bool:
            """@brief 返回无日记 / Return no diary."""

            del args, kwargs
            return False

        async def forbidden_write(*args: object, **kwargs: object) -> None:
            """@brief 记录不应到达的账务写入 / Record a billing write that must not be reached."""

            del args, kwargs
            writes.append("write")

        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.db_connection,
            "fetch_one",
            fake_fetch_one,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "fetch_user_account",
            fake_account,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.conversation_repository,
            "user_diary_exists",
            fake_diary,
        )
        monkeypatch.setattr(
            assistant_turn_acceptance.user_repository,
            "set_coin_balances_and_plan",
            forbidden_write,
        )
        with pytest.raises(IdempotencyConflictError, match="partial durable effect"):
            await PostgresAssistantTurnAcceptanceUoW(  # type: ignore[arg-type]
                workflow,
                RecordingBilling(),  # type: ignore[arg-type]
                administrator_id=ADMINISTRATOR_ID,
            ).accept(_request(), accepted_at=NOW)

        assert writes == []
        assert transaction.exit_exception is IdempotencyConflictError

    asyncio.run(scenario())
