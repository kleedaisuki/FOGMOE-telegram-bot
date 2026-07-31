"""@brief PostgreSQL inference adapter 原子提交测试 / PostgreSQL inference-adapter atomic-commit tests."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from conversation_workflow_testkit import (
    NOW,
    _activity,
    _activity_draft,
    _activity_row,
    _message_draft,
    _message_result,
    _outbound_draft,
    _outbound_row,
    _TransactionContext,
    _turn_row,
)

from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.identity import (
    InferenceActivityId,
    LeaseToken,
    TurnId,
    TurnRevision,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityLease,
    InferenceActivityStatus,
    InferenceFailure,
    InferenceGenerationCause,
    InferenceRetryBudgetCharge,
)
from fogmoe_bot.domain.conversation.message import (
    MessageAppendResult,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    inference as inference_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    outbox as outbox_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow
from fogmoe_bot.infrastructure.database.conversation_workflow.inference import (
    PostgresInferenceRepository,
)


def _claim() -> InferenceActivityClaim:
    """@brief 通过严格工厂构造 processing claim / Build a processing claim through the strict factory.

    @return 测试 claim / Test claim.
    """

    activity = _activity()
    return InferenceActivityClaim.from_processing(
        activity,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(minutes=1),
        cause=InferenceGenerationCause.INITIAL,
    )


def test_processing_activity_requires_an_unfinalized_claim_ordinal() -> None:
    """@brief processing 状态不能把当前 claim 提前计入失败预算 / Processing cannot pre-consume its current claim in the failure budget."""

    with pytest.raises(ValueError, match="unfinalized claim"):
        _activity(retry_budget_used=1)


def test_inference_claim_preserves_conversation_causality_across_workers(
    monkeypatch: Any,
) -> None:
    """@brief 同 Conversation 的后续推理不能越过早期活动 / A later inference cannot overtake an earlier activity in the same Conversation."""

    async def scenario() -> None:
        """@brief 捕获 claim SQL 的会话头部谓词 / Capture the conversation-head predicate in claim SQL."""

        connection = object()
        captured: dict[str, object] = {}

        async def fake_fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> list[tuple[object, ...]]:
            """@brief 记录 claim SQL 且返回空队列 / Record claim SQL and return an empty queue."""

            del params
            captured["sql"] = sql
            captured["connection"] = connection
            return []

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(db, "fetch_all", fake_fetch_all)

        claims = await PostgresInferenceRepository().claim_inference_activities(
            now=NOW,
            limit=8,
            lease_for=timedelta(seconds=30),
        )

        assert claims == ()
        sql = str(captured["sql"])
        assert "earlier.conversation_id = candidate.conversation_id" in sql
        assert (
            "earlier.status IN ('pending', 'processing', 'steer_pending', 'retry')"
            in sql
        )
        assert "(earlier.created_at, earlier.activity_id)" in sql
        assert "< (candidate.created_at, candidate.activity_id)" in sql
        assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
        assert "UPDATE conversation.inference_activities" not in sql
        assert captured["connection"] is connection

    asyncio.run(scenario())


def test_inference_claim_applies_the_domain_transition_before_exact_sql(
    monkeypatch: Any,
) -> None:
    """@brief claim 先经领域转移再精确写入目标标量 / Claim applies the domain transition before persisting exact target scalars."""

    async def scenario() -> None:
        """@brief 验证 lock→domain→UPDATE 顺序与 CAS 字段 / Verify lock-to-domain-to-update ordering and CAS fields.

        @return None / None.
        """

        connection = object()
        repository = PostgresInferenceRepository()
        pending = _activity(status=InferenceActivityStatus.PENDING)
        events: list[str] = []
        captured: dict[str, object] = {}
        original_claim = InferenceActivity.claim

        async def fake_fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> list[tuple[object, ...]]:
            """@brief 返回锁定的领域前态 / Return the locked domain pre-state.

            @param sql 选择 SQL / Selection SQL.
            @param params 选择参数 / Selection parameters.
            @param connection 当前连接 / Current connection.
            @return 单个 pending 活动 / One pending activity.
            """

            events.append("lock")
            captured["select_sql"] = sql
            captured["select_params"] = params
            return [_activity_row(pending)]

        def tracked_claim(
            current: InferenceActivity,
            *,
            token: LeaseToken,
            claimed_at: object,
            lease_expires_at: object,
        ) -> object:
            """@brief 记录领域 claim 并保留决定供 SQL 回读 / Record the domain claim and retain its decision for SQL readback.

            @param current 领取前聚合 / Aggregate before claiming.
            @param token fencing token / Fencing token.
            @param claimed_at 领取时刻 / Claim time.
            @param lease_expires_at lease 截止 / Lease expiry.
            @return 领域 claim capability / Domain claim capability.
            """

            events.append("domain")
            claim = original_claim(
                current,
                token=token,
                claimed_at=claimed_at,  # type: ignore[arg-type]
                lease_expires_at=lease_expires_at,  # type: ignore[arg-type]
            )
            captured["claim"] = claim
            return claim

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[object, ...]:
            """@brief 在领域决定后返回其持久化投影 / Return the persisted projection after the domain decision.

            @param sql 更新 SQL / Update SQL.
            @param params 更新参数 / Update parameters.
            @param connection 当前连接 / Current connection.
            @return processing 目标行 / Processing target row.
            """

            events.append("update")
            captured["update_sql"] = sql
            captured["update_params"] = params
            claim = captured["claim"]
            assert isinstance(claim, InferenceActivityClaim)
            return _activity_row(claim.activity)

        async def fake_load_turn(
            turn_id: TurnId,
            *,
            connection: object,
        ) -> object:
            """@brief 返回等待推理的 Turn / Return a waiting-inference Turn.

            @param turn_id Turn ID / Turn identifier.
            @param connection 当前连接 / Current connection.
            @return waiting-inference Turn / Waiting-inference Turn.
            """

            assert turn_id == pending.turn_id
            return turn_uow._map_turn(
                _turn_row(
                    state="waiting_inference",
                    version=2,
                    inference_attempts=1,
                )
            )

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
        monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(InferenceActivity, "claim", tracked_claim)
        monkeypatch.setattr(
            inference_repository,
            "_load_turn_for_mutation",
            fake_load_turn,
        )

        claims = await repository.claim_inference_activities(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )

        assert len(claims) == 1
        claim = claims[0]
        assert claim.cause is InferenceGenerationCause.INITIAL
        assert events == ["lock", "domain", "update"]
        select_sql = str(captured["select_sql"])
        update_sql = str(captured["update_sql"])
        update_params = captured["update_params"]
        assert isinstance(update_params, tuple)
        assert "FOR UPDATE OF candidate SKIP LOCKED" in select_sql
        assert "UPDATE conversation.inference_activities" not in select_sql
        assert "SET status = %s, version = %s, attempt_count = %s" in update_sql
        assert "AND next_attempt_at IS NOT DISTINCT FROM %s" in update_sql
        assert update_params[:5] == ("processing", 1, 1, 0, None)
        assert update_params[5] == str(claim.token)
        assert update_params[-7:] == (
            "pending",
            0,
            0,
            0,
            0,
            NOW,
            NOW,
        )

    asyncio.run(scenario())


def test_expired_inference_recovery_returns_a_complete_activity_projection(
    monkeypatch: Any,
) -> None:
    """@brief 过期推理租约恢复返回含 traceparent 的完整活动行 / Expired inference recovery returns a complete activity row including traceparent."""

    connection = object()
    repository = PostgresInferenceRepository()
    activity = _activity()
    captured: dict[str, object] = {}
    events: list[str] = []
    token = LeaseToken.new()
    lease_expires_at = NOW + timedelta(seconds=1)
    recovered_at = NOW + timedelta(seconds=2)
    retry_at = recovered_at + timedelta.resolution
    lease = InferenceActivityLease.restore(
        activity,
        token=token,
        lease_expires_at=lease_expires_at,
    )
    target = activity.recover_expired_lease(
        lease,
        recovered_at=recovered_at,
        retry_at=retry_at,
        failure=InferenceFailure("inference worker lease expired before finalization"),
    ).activity
    original_recover = InferenceActivity.recover_expired_lease

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回恢复前后的完整 activity 行 / Return the complete pre/post recovery row.

        @param sql 执行的 SQL / Executed SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 已恢复活动行 / Recovered activity row.
        """

        events.append("lock")
        captured["select_sql"] = sql
        captured["select_params"] = params
        assert connection is not None
        return [(*_activity_row(activity), token.value, lease_expires_at)]

    def tracked_recover(
        current: InferenceActivity,
        capability: InferenceActivityLease,
        *,
        recovered_at: object,
        retry_at: object,
        failure: InferenceFailure,
    ) -> object:
        """@brief 记录领域恢复发生在 SQL UPDATE 前 / Record that domain recovery precedes SQL UPDATE.

        @param current 当前聚合 / Current aggregate.
        @param capability lease capability / Lease capability.
        @param recovered_at 恢复时刻 / Recovery time.
        @param retry_at 重试时刻 / Retry time.
        @param failure 恢复失败摘要 / Recovery failure summary.
        @return 领域恢复决定 / Domain recovery decision.
        """

        events.append("domain")
        return original_recover(
            current,
            capability,
            recovered_at=recovered_at,  # type: ignore[arg-type]
            retry_at=retry_at,  # type: ignore[arg-type]
            failure=failure,
        )

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 捕获领域决定后的精确恢复 UPDATE / Capture the exact recovery UPDATE after the domain decision.

        @param sql 执行的 SQL / Executed SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 领域目标活动行 / Domain-target activity row.
        """

        assert connection is not None
        events.append("update")
        captured["update_sql"] = sql
        captured["update_params"] = params
        return _activity_row(target)

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待推理的 Turn / Return a Turn waiting for inference.

        @param turn_id Turn ID / Turn identifier.
        @param connection 当前连接 / Current connection.
        @return 等待推理 Turn / Waiting-inference Turn.
        """

        assert turn_id == activity.turn_id
        assert connection is not None
        return turn_uow._map_turn(
            _turn_row(
                state="waiting_inference",
                version=3,
                inference_attempts=1,
            )
        )

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 记录恢复后的 Turn / Record the recovered Turn.

        @param turn 已恢复 Turn / Recovered Turn.
        @param expected_version 乐观锁版本 / Optimistic-lock version.
        @param connection 当前连接 / Current connection.
        @return None / None.
        """

        captured["turn"] = turn
        captured["version"] = expected_version
        assert connection is not None

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        InferenceActivity,
        "recover_expired_lease",
        tracked_recover,
    )
    monkeypatch.setattr(inference_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

    recovered = asyncio.run(
        repository.recover_expired_inference_leases(now=recovered_at)
    )

    assert recovered == 1
    assert events == ["lock", "domain", "update"]
    select_sql = str(captured["select_sql"])
    update_sql = str(captured["update_sql"])
    update_params = captured["update_params"]
    assert isinstance(update_params, tuple)
    assert "FOR UPDATE OF candidate SKIP LOCKED" in select_sql
    assert "UPDATE conversation.inference_activities" not in select_sql
    assert "attempt_count = %s" in update_sql
    assert "retry_budget_used = %s" in update_sql
    assert "AND claim_token = CAST(%s AS UUID)" in update_sql
    assert update_params[:5] == (
        "retry",
        target.version,
        target.attempt_count,
        target.retry_budget_used,
        retry_at,
    )
    assert update_params[-2:] == (str(token), lease_expires_at)
    assert getattr(captured["turn"], "state").value == "inference_retry_wait"
    assert captured["version"] == 3


@pytest.mark.parametrize("retry_budget_used", (0, 1))
def test_inference_retry_persists_absolute_retry_budget_under_claim_fence(
    monkeypatch: Any,
    retry_budget_used: int,
) -> None:
    """@brief dependency 保持预算、普通失败只增加一次且受 claim fence 保护 / A dependency preserves budget while an ordinary failure increments it once under the claim fence.

    @param monkeypatch pytest 替换工具 / Pytest replacement helper.
    @param retry_budget_used 本次 retry 后的绝对预算目标 / Absolute budget target after this retry.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 捕获真实 repository retry UPDATE / Capture the real repository retry UPDATE.

        @return None / None.
        """

        connection = object()
        repository = PostgresInferenceRepository()
        claim = _claim()
        failure = claim.activity.record_failure(
            claim,
            failed_at=NOW,
            failure=InferenceFailure("typed retry"),
            budget_charge=(
                InferenceRetryBudgetCharge.PRESERVE
                if retry_budget_used == 0
                else InferenceRetryBudgetCharge.CONSUME
            ),
        )
        decision = failure.schedule_retry(retry_at=NOW + timedelta(seconds=5))
        captured: dict[str, object] = {}

        async def fake_load_activity(
            activity_id: InferenceActivityId,
            *,
            connection: object,
        ) -> tuple[InferenceActivity, LeaseToken, object]:
            """@brief 返回当前 processing claim / Return the current processing claim."""

            assert activity_id == claim.activity.activity_id
            return claim.activity, claim.token, claim.lease_expires_at

        async def fake_load_turn(
            turn_id: TurnId,
            *,
            connection: object,
        ) -> object:
            """@brief 返回 waiting-inference Turn / Return a waiting-inference Turn."""

            assert turn_id == claim.activity.turn_id
            return turn_uow._map_turn(
                _turn_row(
                    state="waiting_inference",
                    version=2,
                    inference_attempts=1,
                )
            )

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[str]:
            """@brief 捕获 fenced retry UPDATE / Capture the fenced retry UPDATE."""

            captured["sql"] = sql
            captured["params"] = params
            return ("retry-row",)

        def fake_map_activity(row: object) -> InferenceActivity:
            """@brief 返回领域重试目标 / Return the domain retry target."""

            assert row == ("retry-row",)
            return decision.activity

        async def fake_persist(
            turn: object,
            *,
            expected_version: int,
            connection: object,
        ) -> None:
            """@brief 记录 retry Turn 已同步 / Record synchronization of the retry Turn."""

            captured["turn"] = turn
            captured["turn_version"] = expected_version

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(
            repository,
            "_load_inference_activity_for_update",
            fake_load_activity,
        )
        monkeypatch.setattr(
            inference_repository,
            "_load_turn_for_mutation",
            fake_load_turn,
        )
        monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(
            inference_repository,
            "_map_inference_activity",
            fake_map_activity,
        )
        monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

        await repository.retry_inference_activity(decision)

        sql = str(captured["sql"])
        params = captured["params"]
        assert isinstance(params, tuple)
        assert "retry_budget_used = %s" in sql
        assert "AND retry_budget_used = %s" in sql
        assert "AND version = %s" in sql
        assert params[4] == retry_budget_used
        assert params[-1] == 0
        assert getattr(captured["turn"], "state").value == "inference_retry_wait"

    asyncio.run(scenario())


def test_inference_final_failure_persists_retry_budget_under_claim_fence(
    monkeypatch: Any,
) -> None:
    """@brief 最终失败原子写入绝对预算并校验旧预算 fence / Final failure atomically writes the absolute budget and fences the prior budget."""

    async def scenario() -> None:
        """@brief 捕获真实 repository final-failure UPDATE / Capture the real repository final-failure UPDATE."""

        connection = object()
        repository = PostgresInferenceRepository()
        claim = _claim()
        failure = claim.activity.record_failure(
            claim,
            failed_at=NOW,
            failure=InferenceFailure("typed final failure"),
            budget_charge=InferenceRetryBudgetCharge.CONSUME,
        )
        decision = failure.fail_final()
        assistant_message = _message_draft(role=MessageRole.ASSISTANT)
        outbound = _outbound_draft()
        captured: dict[str, object] = {}

        async def fake_load_activity(
            activity_id: InferenceActivityId,
            *,
            connection: object,
        ) -> tuple[InferenceActivity, LeaseToken, object]:
            """@brief 返回当前 processing claim / Return the current processing claim."""

            assert activity_id == claim.activity.activity_id
            return claim.activity, claim.token, claim.lease_expires_at

        async def fake_load_turn(
            turn_id: TurnId,
            *,
            connection: object,
        ) -> object:
            """@brief 返回 waiting-inference Turn / Return a waiting-inference Turn."""

            assert turn_id == claim.activity.turn_id
            return turn_uow._map_turn(
                _turn_row(
                    state="waiting_inference",
                    version=2,
                    inference_attempts=1,
                )
            )

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[str] | None:
            """@brief 模拟 advisory lock 并捕获 failed UPDATE / Simulate the advisory lock and capture the failed UPDATE."""

            if "pg_advisory_xact_lock" in sql:
                return None
            captured["sql"] = sql
            captured["params"] = params
            return ("failed-row",)

        def fake_map_activity(row: object) -> InferenceActivity:
            """@brief 返回含目标预算的 failed 投影 / Return a failed projection carrying the target budget."""

            assert row == ("failed-row",)
            return decision.activity

        async def fake_append(
            message: MessageDraft,
            *,
            connection: object,
        ) -> MessageAppendResult:
            """@brief 返回已原子追加的失败消息 / Return the atomically appended failure message."""

            return _message_result(message, inserted=True)

        async def fake_enqueue(
            connection: object,
            draft: OutboundDraft,
        ) -> OutboundEnqueueResult:
            """@brief 返回已原子入队的失败反馈 / Return the atomically enqueued failure feedback."""

            assert draft == outbound
            return OutboundEnqueueResult(
                message=outbox_repository._map_outbound(
                    _outbound_row(status="pending")
                ),
                inserted=True,
            )

        async def fake_persist(
            turn: object,
            *,
            expected_version: int,
            connection: object,
        ) -> None:
            """@brief 记录最终失败 Turn 已同步 / Record synchronization of the final-failure Turn."""

            captured["turn"] = turn
            captured["turn_version"] = expected_version

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(
            repository,
            "_load_inference_activity_for_update",
            fake_load_activity,
        )
        monkeypatch.setattr(
            inference_repository,
            "_load_turn_for_mutation",
            fake_load_turn,
        )
        monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(
            inference_repository,
            "_map_inference_activity",
            fake_map_activity,
        )
        monkeypatch.setattr(inference_repository, "_append_message", fake_append)
        monkeypatch.setattr(
            repository._outbox,
            "enqueue_outbound_in_transaction",
            fake_enqueue,
        )
        monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

        result = await repository.fail_inference_activity(
            decision,
            assistant_message=assistant_message,
            outbounds=(outbound,),
        )

        sql = str(captured["sql"])
        params = captured["params"]
        assert isinstance(params, tuple)
        assert "retry_budget_used = %s" in sql
        assert "AND retry_budget_used = %s" in sql
        assert "AND version = %s" in sql
        assert params[3] == 1
        assert params[-1] == 0
        assert result.activity.retry_budget_used == 1
        assert getattr(captured["turn"], "state").value == "waiting_delivery"

    asyncio.run(scenario())


def test_inference_uow_failure_exits_the_single_transaction_for_rollback(
    monkeypatch: Any,
) -> None:
    """@brief outbox 写入失败会让整个推理 UoW 退出并回滚 / An outbox failure exits and rolls back the entire inference unit of work."""

    connection = object()
    transaction = _TransactionContext(connection)
    repository = PostgresInferenceRepository()
    assistant_message = _message_draft(role=MessageRole.ASSISTANT)
    outbound = _outbound_draft()
    claim = _claim()
    decision = claim.activity.succeed(claim, completed_at=NOW)
    persisted = False

    async def fake_load_activity(
        activity_id: InferenceActivityId,
        *,
        connection: object,
    ) -> tuple[InferenceActivity, LeaseToken, object]:
        """@brief 返回当前 processing 活动 / Return the current processing activity."""

        return claim.activity, claim.token, claim.lease_expires_at

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回待推理回合 / Return a turn waiting for inference."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_inference",
                version=2,
                inference_attempts=1,
            )
        )

    async def fake_append(
        message: MessageDraft,
        *,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 模拟助手消息已在事务中追加 / Simulate assistant-message append in the transaction."""

        return _message_result(message, inserted=True)

    async def fail_enqueue(
        connection: object,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 模拟 outbox 写入失败 / Simulate an outbox write failure."""

        raise RuntimeError("outbox insert failed")

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 记录不应发生的回合持久化 / Record turn persistence that must not occur."""

        nonlocal persisted
        persisted = True

    async def fake_advisory_lock(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 模拟 conversation advisory lock / Simulate the conversation advisory lock."""

        assert "pg_advisory_xact_lock" in sql
        assert params == ("telegram:chat:-100:user:42:thread:9",)

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        db,
        "fetch_one",
        fake_advisory_lock,
    )
    monkeypatch.setattr(
        repository,
        "_load_inference_activity_for_update",
        fake_load_activity,
    )
    monkeypatch.setattr(inference_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(inference_repository, "_append_message", fake_append)
    monkeypatch.setattr(
        repository._outbox,
        "enqueue_outbound_in_transaction",
        fail_enqueue,
    )
    monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        asyncio.run(
            repository.complete_inference_activity(
                decision,
                assistant_message=assistant_message,
                outbounds=(outbound,),
            )
        )

    assert isinstance(transaction.exception, RuntimeError)
    assert persisted is False


def test_inference_completion_preserves_atomic_effect_commit_order(
    monkeypatch: Any,
) -> None:
    """@brief completion 保持消息、outbox、activity、Turn 的提交顺序 / Completion preserves message, outbox, activity, then Turn commit order."""

    async def scenario() -> None:
        """@brief 执行成功组合提交并记录写入顺序 / Execute a successful composite commit and record write order.

        @return None / None.
        """

        connection = object()
        repository = PostgresInferenceRepository()
        assistant_message = _message_draft(role=MessageRole.ASSISTANT)
        outbound = _outbound_draft()
        claim = _claim()
        decision = claim.activity.succeed(claim, completed_at=NOW)
        events: list[str] = []

        async def fake_load_activity(
            activity_id: InferenceActivityId,
            *,
            connection: object,
        ) -> tuple[InferenceActivity, LeaseToken, object]:
            """@brief 返回当前 claim-owned processing 活动 / Return the current claim-owned processing activity.

            @param activity_id 活动 ID / Activity identifier.
            @param connection 当前连接 / Current connection.
            @return 聚合、token 与 lease / Aggregate, token, and lease.
            """

            assert activity_id == claim.activity.activity_id
            return claim.activity, claim.token, claim.lease_expires_at

        async def fake_load_turn(
            turn_id: TurnId,
            *,
            connection: object,
        ) -> object:
            """@brief 返回 waiting-inference Turn / Return a waiting-inference Turn.

            @param turn_id Turn ID / Turn identifier.
            @param connection 当前连接 / Current connection.
            @return waiting-inference Turn / Waiting-inference Turn.
            """

            assert turn_id == claim.activity.turn_id
            return turn_uow._map_turn(
                _turn_row(
                    state="waiting_inference",
                    version=2,
                    inference_attempts=1,
                )
            )

        async def fake_append(
            message: MessageDraft,
            *,
            connection: object,
        ) -> MessageAppendResult:
            """@brief 记录 assistant message 写入 / Record assistant-message persistence.

            @param message 助手消息 / Assistant message.
            @param connection 当前连接 / Current connection.
            @return 消息回执 / Message receipt.
            """

            events.append("message")
            return _message_result(message, inserted=True)

        async def fake_enqueue(
            connection: object,
            draft: OutboundDraft,
        ) -> OutboundEnqueueResult:
            """@brief 记录 outbox 写入 / Record outbox persistence.

            @param connection 当前连接 / Current connection.
            @param draft 出站草稿 / Outbound draft.
            @return outbox 回执 / Outbox receipt.
            """

            assert draft == outbound
            events.append("outbox")
            return OutboundEnqueueResult(
                message=outbox_repository._map_outbound(
                    _outbound_row(status="pending")
                ),
                inserted=True,
            )

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[str] | None:
            """@brief 模拟 advisory lock 并记录 activity completion / Simulate the advisory lock and record activity completion.

            @param sql SQL 文本 / SQL text.
            @param params SQL 参数 / SQL parameters.
            @param connection 当前连接 / Current connection.
            @return activity 占位行或 None / Activity placeholder row or None.
            """

            if "pg_advisory_xact_lock" in sql:
                return None
            events.append("activity")
            assert "AND version = %s" in sql
            assert params[0] == decision.activity.version
            return ("completed-row",)

        def fake_map_activity(row: object) -> InferenceActivity:
            """@brief 将 UPDATE 回读映射为领域目标 / Map UPDATE readback to the domain target.

            @param row 数据库占位行 / Database placeholder row.
            @return completed 领域聚合 / Completed domain aggregate.
            """

            assert row == ("completed-row",)
            return decision.activity

        async def fake_persist(
            turn: object,
            *,
            expected_version: int,
            connection: object,
        ) -> None:
            """@brief 记录 Turn 最终写入 / Record final Turn persistence.

            @param turn waiting-delivery Turn / Waiting-delivery Turn.
            @param expected_version 旧 Turn 版本 / Previous Turn version.
            @param connection 当前连接 / Current connection.
            @return None / None.
            """

            assert getattr(turn, "state").value == "waiting_delivery"
            assert expected_version == 2
            events.append("turn")

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(
            repository,
            "_load_inference_activity_for_update",
            fake_load_activity,
        )
        monkeypatch.setattr(
            inference_repository,
            "_load_turn_for_mutation",
            fake_load_turn,
        )
        monkeypatch.setattr(inference_repository, "_append_message", fake_append)
        monkeypatch.setattr(
            repository._outbox,
            "enqueue_outbound_in_transaction",
            fake_enqueue,
        )
        monkeypatch.setattr(
            inference_repository,
            "_map_inference_activity",
            fake_map_activity,
        )
        monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

        result = await repository.complete_inference_activity(
            decision,
            assistant_message=assistant_message,
            outbounds=(outbound,),
        )

        assert events == ["message", "outbox", "activity", "turn"]
        assert result.activity == decision.activity

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("current_state", "current_version", "outbound_status"),
    (
        ("waiting_delivery", 4, "pending"),
        ("delivered", 5, "delivered"),
    ),
)
def test_inference_uow_replay_returns_existing_atomic_effects(
    monkeypatch: Any,
    current_state: str,
    current_version: int,
    outbound_status: str,
) -> None:
    """@brief 已提交推理 UoW 的重放返回规范消息而不再次写入 / Replay of a committed inference unit returns canonical effects without rewriting."""

    connection = object()
    repository = PostgresInferenceRepository()
    assistant_message = _message_draft(role=MessageRole.ASSISTANT)
    outbound = _outbound_draft()
    claim = _claim()
    decision = claim.activity.succeed(claim, completed_at=NOW)

    async def fake_load_activity(
        activity_id: InferenceActivityId,
        *,
        connection: object,
    ) -> tuple[InferenceActivity, None, None]:
        """@brief 返回已完成活动 / Return the completed activity."""

        return (
            decision.activity,
            None,
            None,
        )

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回已完成组合提交的回合 / Return a turn whose composite commit already completed."""

        return turn_uow._map_turn(
            _turn_row(
                state=current_state,
                version=current_version,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def existing_message(
        draft: MessageDraft,
        *,
        operation: str,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 返回已存在助手消息 / Return the existing assistant message."""

        return _message_result(draft, inserted=False)

    async def existing_outbound(
        connection: object,
        draft: OutboundDraft,
        *,
        operation: str,
    ) -> OutboundEnqueueResult:
        """@brief 返回已存在 outbox 消息 / Return the existing outbox message."""

        return OutboundEnqueueResult(
            message=outbox_repository._map_outbound(
                _outbound_row(status=outbound_status)
            ),
            inserted=False,
        )

    async def unexpected_write(*args: object, **kwargs: object) -> None:
        """@brief 拒绝幂等重放中的写操作 / Reject writes during idempotent replay."""

        raise AssertionError("idempotent replay attempted a write")

    async def fake_advisory_lock(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 模拟 completion 的 conversation lock / Simulate completion's conversation lock."""

        assert "pg_advisory_xact_lock" in sql
        assert params == ("telegram:chat:-100:user:42:thread:9",)

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(
        db,
        "fetch_one",
        fake_advisory_lock,
    )
    monkeypatch.setattr(
        repository,
        "_load_inference_activity_for_update",
        fake_load_activity,
    )
    monkeypatch.setattr(inference_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(
        inference_repository, "_require_existing_message", existing_message
    )
    monkeypatch.setattr(
        repository._outbox,
        "require_existing_outbound_in_transaction",
        existing_outbound,
    )
    monkeypatch.setattr(inference_repository, "_append_message", unexpected_write)
    monkeypatch.setattr(
        repository._outbox,
        "enqueue_outbound_in_transaction",
        unexpected_write,
    )
    monkeypatch.setattr(inference_repository, "_persist_turn", unexpected_write)

    result = asyncio.run(
        repository.complete_inference_activity(
            decision,
            assistant_message=assistant_message,
            outbounds=(outbound,),
        )
    )

    assert result.turn.state.value == current_state
    assert result.assistant_message.inserted is False
    assert result.outbounds[0].inserted is False


def test_same_token_completion_replay_rejects_a_non_successor_version(
    monkeypatch: Any,
) -> None:
    """@brief 相同 token 不能掩盖 completion 版本越界 / The same token cannot conceal an out-of-sequence completion version."""

    connection = object()
    repository = PostgresInferenceRepository()
    claim = _claim()
    decision = claim.activity.succeed(claim, completed_at=NOW)
    wrong_version = InferenceActivity.restore(
        draft=_activity_draft(),
        status=InferenceActivityStatus.COMPLETED,
        version=decision.activity.version + 1,
        attempt_count=decision.activity.attempt_count,
        retry_budget_used=decision.activity.retry_budget_used,
        next_attempt_at=None,
        updated_at=NOW,
        completed_at=NOW,
        completion_token=claim.token,
        last_error=None,
        input_revision=TurnRevision.initial(),
    )

    async def fake_load_activity(
        activity_id: InferenceActivityId,
        *,
        connection: object,
    ) -> tuple[InferenceActivity, None, None]:
        """@brief 返回同 token 但错误版本的 completed 活动 / Return a completed activity with the same token but wrong version.

        @param activity_id 活动 ID / Activity identifier.
        @param connection 当前连接 / Current connection.
        @return 错序 completed 活动 / Out-of-sequence completed activity.
        """

        assert activity_id == claim.activity.activity_id
        return wrong_version, None, None

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回 completion 后 Turn / Return the post-completion Turn.

        @param turn_id Turn ID / Turn identifier.
        @param connection 当前连接 / Current connection.
        @return waiting-delivery Turn / Waiting-delivery Turn.
        """

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=4,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def fake_advisory_lock(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 模拟 completion conversation lock / Simulate the completion conversation lock.

        @param sql advisory-lock SQL / Advisory-lock SQL.
        @param params lock 参数 / Lock parameters.
        @param connection 当前连接 / Current connection.
        @return None / None.
        """

        assert "pg_advisory_xact_lock" in sql

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_one", fake_advisory_lock)
    monkeypatch.setattr(
        repository,
        "_load_inference_activity_for_update",
        fake_load_activity,
    )
    monkeypatch.setattr(
        inference_repository,
        "_load_turn_for_mutation",
        fake_load_turn,
    )

    with pytest.raises(StaleClaimError, match="Stale inference claim"):
        asyncio.run(
            repository.complete_inference_activity(
                decision,
                assistant_message=_message_draft(role=MessageRole.ASSISTANT),
                outbounds=(_outbound_draft(),),
            )
        )
