"""@brief AdminRuntime 崩溃重放与资源边界测试 / AdminRuntime crash-replay and resource-bound tests."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.application.admin.runtime import AdminRuntime
from fogmoe_bot.application.admin.models import (
    AnnouncementAcceptance,
    RequestAnnouncement,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementClaimToken,
    AnnouncementRecipient,
    AnnouncementRecipientClaim,
    AnnouncementRecipientDeadLettered,
    AnnouncementRecipientExpanded,
    AnnouncementRecipientKind,
    AnnouncementRecipientRetryScheduled,
    AnnouncementRecipientStatus,
    FailedAnnouncementRecipient,
    RetryWaitingAnnouncementRecipient,
)
from fogmoe_bot.presentation.telegram.admin_handlers import (
    TelegramAnnouncementOutboundFactory,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 worker 时间 / Fixed worker instant."""


class FixedClock:
    """@brief 可控 UTC 时钟 / Controllable UTC clock."""

    def __init__(self, events: list[str] | None = None) -> None:
        """@brief 初始化固定时间 / Initialize the fixed instant.

        @param events 可选调用顺序记录 / Optional call-order recording.
        """

        self.value = NOW
        """@brief 当前 UTC 时间 / Current UTC instant."""
        self.events = events
        """@brief 可选调用顺序记录 / Optional call-order recording."""

    def now(self) -> datetime:
        """@brief 返回当前时间 / Return current time.

        @return aware UTC 时间 / Aware UTC instant.
        """

        if self.events is not None:
            self.events.append("clock")
        return self.value


class RecordingOutbound:
    """@brief 以确定性 identity 去重的测试 outbox / Test outbox deduplicating by deterministic identity."""

    def __init__(
        self,
        events: list[str] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        """@brief 初始化记录 / Initialize recordings.

        @param events 可选调用顺序记录 / Optional call-order recording.
        @param fail 是否注入 enqueue 失败 / Whether to inject an enqueue failure.
        """

        self.calls: list[StandaloneOutboundCommand] = []
        """@brief 所有 enqueue 尝试 / Every enqueue attempt."""
        self.effects: dict[tuple[str, str], StandaloneOutboundCommand] = {}
        """@brief 已提交语义副作用 / Committed semantic effects."""
        self.events = events
        """@brief 可选调用顺序记录 / Optional call-order recording."""
        self.fail = fail
        """@brief 是否注入 enqueue 失败 / Whether to inject an enqueue failure."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 幂等记录命令 / Idempotently record a command.

        @param command 出站命令 / Outbound command.
        @return None / None.
        """

        if self.events is not None:
            self.events.append("outbox")
        if self.fail:
            raise RuntimeError("temporary outbound failure")
        self.calls.append(command)
        self.effects.setdefault(
            (str(command.conversation_id), command.idempotency_key), command
        )


class ScriptedOperations:
    """@brief 可脚本化公告回执端口 / Scriptable announcement-receipt port."""

    def __init__(
        self,
        claim_batches: list[tuple[AnnouncementRecipientClaim, ...]],
        events: list[str] | None = None,
    ) -> None:
        """@brief 注入每轮领取 / Inject claim batches per pass.

        @param claim_batches 领取脚本 / Claim script.
        @param events 可选调用顺序记录 / Optional call-order recording.
        """

        self.claim_batches = claim_batches
        """@brief 剩余领取脚本 / Remaining claim script."""
        self.mark_calls: list[AnnouncementRecipientExpanded] = []
        """@brief 终结调用 / Finalization calls."""
        self.retry_calls: list[AnnouncementRecipientRetryScheduled] = []
        """@brief 重试调用 / Retry calls."""
        self.fail_calls: list[AnnouncementRecipientDeadLettered] = []
        """@brief 最终失败调用 / Final-failure calls."""
        self.cancel_first_mark = False
        """@brief 首次终结时模拟 kill-9 取消 / Simulate kill-9 cancellation on first finalization."""
        self.recover_calls = 0
        """@brief 租约恢复调用数 / Lease-recovery call count."""
        self.promote_calls = 0
        """@brief 投递完成推进调用数 / Delivery-completion promotion call count."""
        self.claim_calls = 0
        """@brief 公告领取调用数 / Announcement-claim call count."""
        self.events = events
        """@brief 可选调用顺序记录 / Optional call-order recording."""

    async def accept(self, command: RequestAnnouncement) -> AnnouncementAcceptance:
        """@brief 拒绝此 runtime 测试范围外的公告创建 / Reject announcement creation outside this runtime-test scope.

        @param command 公告请求 / Announcement request.
        @return 永不返回 / Never returns.
        @raise AssertionError 此测试端口不应创建公告 / Raised because this test port must not create announcements.
        """

        del command
        raise AssertionError("accept is outside the AdminRuntime test boundary")

    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        """@brief 模拟无过期租约 / Simulate no expired leases.

        @param now 当前时间 / Current instant.
        @param limit 批量上限 / Batch limit.
        @return 零 / Zero.
        """

        del now, limit
        self.recover_calls += 1
        return 0

    async def promote_delivery_completions(self, *, now: datetime, limit: int) -> int:
        """@brief 模拟无完成推进 / Simulate no completion promotion.

        @param now 当前时间 / Current instant.
        @param limit 批量上限 / Batch limit.
        @return 零 / Zero.
        """

        del now, limit
        if self.events is not None:
            self.events.append("promote")
        self.promote_calls += 1
        return 0

    async def claim_ready(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> tuple[AnnouncementRecipientClaim, ...]:
        """@brief 返回下一批领取 / Return the next claim batch.

        @param now 当前时间 / Current instant.
        @param lease_for 租约 / Lease duration.
        @param limit 批量上限 / Batch limit.
        @return 下一批 / Next batch.
        """

        del now, lease_for
        if self.events is not None:
            self.events.append("claim")
        self.claim_calls += 1
        batch = self.claim_batches.pop(0) if self.claim_batches else ()
        return batch[:limit]

    async def persist_expanded(
        self,
        decision: AnnouncementRecipientExpanded,
    ) -> bool:
        """@brief 记录终结或模拟取消 / Record finalization or simulate cancellation.

        @param decision expanded 领域决策 / Expanded domain decision.
        @return True / True.
        """

        if self.cancel_first_mark:
            self.cancel_first_mark = False
            raise asyncio.CancelledError
        if self.events is not None:
            self.events.append("persist_expanded")
        self.mark_calls.append(decision)
        return True

    async def persist_retry(
        self,
        decision: AnnouncementRecipientRetryScheduled,
    ) -> bool:
        """@brief 记录重试 / Record a retry.

        @param decision retry-wait 领域决策 / Retry-wait domain decision.
        @return True / True.
        """

        if self.events is not None:
            self.events.append("persist_retry")
        self.retry_calls.append(decision)
        return True

    async def persist_dead_letter(
        self,
        decision: AnnouncementRecipientDeadLettered,
    ) -> bool:
        """@brief 记录最终失败 / Record a final failure.

        @param decision failed-final 领域决策 / Failed-final domain decision.
        @return True / True.
        """

        if self.events is not None:
            self.events.append("persist_dead_letter")
        self.fail_calls.append(decision)
        return True


class RecordingFactory:
    """@brief 记录 factory 调用顺序的 Telegram 代理 / Telegram factory proxy recording call order."""

    def __init__(self, events: list[str]) -> None:
        """@brief 保存顺序记录 / Store the call-order recording.

        @param events 共享顺序记录 / Shared call-order recording.
        """

        self._events = events
        self._delegate = TelegramAnnouncementOutboundFactory()

    def build(self, claim: AnnouncementRecipientClaim) -> StandaloneOutboundCommand:
        """@brief 记录并委托构造 / Record and delegate construction.

        @param claim 领取能力 / Claim capability.
        @return Telegram outbox 命令 / Telegram outbox command.
        """

        self._events.append("factory")
        return self._delegate.build(claim)


class PeriodicRecoveryOperations(ScriptedOperations):
    """@brief 记录独立租约恢复 cadence 的操作端口 / Operations port recording the independent lease-recovery cadence."""

    def __init__(self, stop_event: asyncio.Event, *, fail_first: bool) -> None:
        """@brief 配置第二轮恢复后停止 / Configure a stop after the second recovery pass.

        @param stop_event 结构化停止事件 / Structured stop event.
        @param fail_first 首次恢复是否失败 / Whether the first recovery fails.
        """

        super().__init__([])
        self._stop_event = stop_event
        self._fail_first = fail_first
        self.recovery_times: list[float] = []
        """@brief 恢复调用的单调时间 / Monotonic instants of recovery calls."""
        self.recovery_tasks: list[str] = []
        """@brief 恢复调用的任务名 / Task names owning recovery calls."""

    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        """@brief 记录恢复并可选注入首轮故障 / Record recovery and optionally inject a first-pass failure.

        @param now 当前时间 / Current instant.
        @param limit 批量上限 / Batch limit.
        @return 零 / Zero.
        @raise RuntimeError 注入的首轮故障 / Injected first-pass failure.
        """

        del now, limit
        self.recover_calls += 1
        self.recovery_times.append(time.monotonic())
        task = asyncio.current_task()
        self.recovery_tasks.append(task.get_name() if task is not None else "")
        if self._fail_first and self.recover_calls == 1:
            raise RuntimeError("transient recovery failure")
        if self.recover_calls >= 2:
            self._stop_event.set()
        return 0


def _claim(
    *, token: object | None = None, attempt: int = 1
) -> AnnouncementRecipientClaim:
    """@brief 构造受众领取 / Build an audience claim.

    @param token 可选占位，用于强制新 token / Optional placeholder forcing a new token.
    @param attempt 尝试序号 / Attempt number.
    @return 领取 / Claim.
    """

    del token
    previous_attempts = attempt - 1
    recipient = AnnouncementRecipient.restore(
        announcement_id=AnnouncementId.for_idempotency_key("announcement:1"),
        recipient_kind=AnnouncementRecipientKind.USER,
        chat_id=42,
        message_thread_id=None,
        reply_to_message_id=None,
        status=(
            AnnouncementRecipientStatus.PENDING
            if previous_attempts == 0
            else AnnouncementRecipientStatus.RETRY_WAIT
        ),
        attempt_count=previous_attempts,
        next_attempt_at=NOW,
        claim_token=None,
        lease_expires_at=None,
        outbound_message_id=None,
        last_error=None if previous_attempts == 0 else "previous_error",
        created_at=NOW,
        updated_at=NOW,
        expanded_at=None,
        terminal_at=None,
    )
    return recipient.claim(
        token=AnnouncementClaimToken.new(),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
        content=AnnouncementDispatchContent(
            body="hello",
            counts=AnnouncementDeliveryCounts(
                recipients=1,
                delivered=0,
                failed=0,
            ),
            announcement_created_at=NOW,
        ),
    )


def test_kill_after_outbox_commit_replays_one_semantic_effect() -> None:
    """@brief outbox 提交后 kill-9 的重放仍只有一个语义副作用 / Replay after kill-9 following outbox commit yields one semantic effect."""

    first = _claim(attempt=1)
    second = _claim(token=object(), attempt=2)
    operations = ScriptedOperations([(first,), (second,)])
    operations.cancel_first_mark = True
    outbound = RecordingOutbound()
    runtime = AdminRuntime(
        operations=operations,
        outbound=outbound,
        factory=TelegramAnnouncementOutboundFactory(),
        clock=FixedClock(),
        batch_size=1,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.run_once())
    assert asyncio.run(runtime.run_once()) == 1

    assert len(outbound.calls) == 2
    assert len(outbound.effects) == 1
    assert outbound.calls[0] == outbound.calls[1]
    assert len(operations.mark_calls) == 1
    assert (
        operations.mark_calls[0].claim.capability.token
        == second.capability.token
    )


def test_run_once_is_a_deterministic_business_pass_without_recovery() -> None:
    """@brief run_once 只推进完成与领取，不隐式恢复租约 / run_once only promotes and claims without hidden lease recovery."""

    operations = ScriptedOperations([])
    runtime = AdminRuntime(
        operations=operations,
        outbound=RecordingOutbound(),
        factory=TelegramAnnouncementOutboundFactory(),
        clock=FixedClock(),
    )

    assert asyncio.run(runtime.run_once()) == 0
    assert operations.recover_calls == 0
    assert operations.promote_calls == 1
    assert operations.claim_calls == 1


def test_success_preserves_clock_factory_outbox_and_persistence_order() -> None:
    """@brief 成功路径保持既有 clock/outbox 调用顺序 / Success preserves the established clock/outbox call order."""

    events: list[str] = []
    operations = ScriptedOperations([(_claim(),)], events)
    runtime = AdminRuntime(
        operations=operations,
        outbound=RecordingOutbound(events),
        factory=RecordingFactory(events),
        clock=FixedClock(events),
    )

    assert asyncio.run(runtime.run_once()) == 1
    assert events == [
        "clock",
        "promote",
        "claim",
        "factory",
        "outbox",
        "clock",
        "persist_expanded",
    ]


@pytest.mark.parametrize(
    ("attempt", "expected_event"),
    ((1, "persist_retry"), (8, "persist_dead_letter")),
)
def test_failure_policy_produces_typed_retry_or_dead_letter(
    attempt: int,
    expected_event: str,
) -> None:
    """@brief runtime 仅选择 capped retry 或最终失败策略 / Runtime only selects capped retry or final-failure policy.

    @param attempt 当前尝试序号 / Current attempt number.
    @param expected_event 预期持久化决策 / Expected persistence decision.
    """

    events: list[str] = []
    operations = ScriptedOperations([(_claim(attempt=attempt),)], events)
    runtime = AdminRuntime(
        operations=operations,
        outbound=RecordingOutbound(events, fail=True),
        factory=RecordingFactory(events),
        clock=FixedClock(events),
        max_attempts=8,
        initial_retry=timedelta(seconds=1),
    )

    assert asyncio.run(runtime.run_once()) == 1
    assert events == [
        "clock",
        "promote",
        "claim",
        "factory",
        "outbox",
        "clock",
        expected_event,
    ]
    if attempt == 1:
        state = operations.retry_calls[0].recipient.state
        assert isinstance(state, RetryWaitingAnnouncementRecipient)
        assert state.next_attempt_at == NOW + timedelta(seconds=1)
        assert state.failure.value == "RuntimeError"
        assert operations.fail_calls == []
    else:
        state = operations.fail_calls[0].recipient.state
        assert isinstance(state, FailedAnnouncementRecipient)
        assert state.terminal_at == NOW
        assert state.failure.value == "RuntimeError"
        assert operations.retry_calls == []


@pytest.mark.parametrize("fail_first", [False, True], ids=["steady", "retry"])
def test_recovery_cadence_is_independent_of_business_polling(
    fail_first: bool,
) -> None:
    """@brief 单 owner 恢复不受长业务轮询影响且可隔离短暂故障 / Single-owner recovery is independent of long business polling and isolates transient faults.

    @param fail_first 首轮恢复是否注入故障 / Whether the first recovery attempt fails.
    """

    async def scenario() -> None:
        """@brief 观察立即恢复与租约半程 cadence / Observe immediate recovery and the half-lease cadence."""

        stop_event = asyncio.Event()
        operations = PeriodicRecoveryOperations(stop_event, fail_first=fail_first)
        runtime = AdminRuntime(
            operations=operations,
            outbound=RecordingOutbound(),
            factory=TelegramAnnouncementOutboundFactory(),
            clock=FixedClock(),
            poll_interval=2.0,
            lease_for=timedelta(milliseconds=200),
        )

        started = time.monotonic()
        await asyncio.wait_for(runtime.run(stop_event), timeout=1)

        assert len(operations.recovery_times) == 2
        assert operations.recovery_times[0] - started < 0.5
        recovery_gap = operations.recovery_times[1] - operations.recovery_times[0]
        assert 0.075 <= recovery_gap < 0.5
        assert set(operations.recovery_tasks) == {"admin-announcement-recovery"}
        assert operations.promote_calls == 1
        assert operations.claim_calls == 1

    asyncio.run(scenario())


def test_runtime_constructor_rejects_unbounded_configuration() -> None:
    """@brief worker 拒绝无界或非法资源配置 / Worker rejects unbounded or invalid resource configuration."""

    with pytest.raises(ValueError, match="positive"):
        AdminRuntime(
            operations=ScriptedOperations([]),
            outbound=RecordingOutbound(),
            factory=TelegramAnnouncementOutboundFactory(),
            batch_size=0,
        )
