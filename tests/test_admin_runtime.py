"""@brief AdminRuntime 崩溃重放与资源边界测试 / AdminRuntime crash-replay and resource-bound tests."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fogmoe_bot.application.admin.runtime import AdminRuntime
from fogmoe_bot.application.admin.models import (
    AnnouncementAcceptance,
    RequestAnnouncement,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.admin import (
    AnnouncementId,
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.presentation.telegram.admin_handlers import (
    TelegramAnnouncementOutboundFactory,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 worker 时间 / Fixed worker instant."""


class FixedClock:
    """@brief 可控 UTC 时钟 / Controllable UTC clock."""

    def __init__(self) -> None:
        """@brief 初始化固定时间 / Initialize the fixed instant."""

        self.value = NOW
        """@brief 当前 UTC 时间 / Current UTC instant."""

    def now(self) -> datetime:
        """@brief 返回当前时间 / Return current time.

        @return aware UTC 时间 / Aware UTC instant.
        """

        return self.value


class RecordingOutbound:
    """@brief 以确定性 identity 去重的测试 outbox / Test outbox deduplicating by deterministic identity."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.calls: list[StandaloneOutboundCommand] = []
        """@brief 所有 enqueue 尝试 / Every enqueue attempt."""
        self.effects: dict[tuple[str, str], StandaloneOutboundCommand] = {}
        """@brief 已提交语义副作用 / Committed semantic effects."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 幂等记录命令 / Idempotently record a command.

        @param command 出站命令 / Outbound command.
        @return None / None.
        """

        self.calls.append(command)
        self.effects.setdefault(
            (str(command.conversation_id), command.idempotency_key), command
        )


class ScriptedOperations:
    """@brief 可脚本化公告回执端口 / Scriptable announcement-receipt port."""

    def __init__(
        self, claim_batches: list[tuple[AnnouncementRecipientClaim, ...]]
    ) -> None:
        """@brief 注入每轮领取 / Inject claim batches per pass.

        @param claim_batches 领取脚本 / Claim script.
        """

        self.claim_batches = claim_batches
        """@brief 剩余领取脚本 / Remaining claim script."""
        self.mark_calls: list[tuple[AnnouncementRecipientClaim, OutboundMessageId]] = []
        """@brief 终结调用 / Finalization calls."""
        self.retry_calls: list[AnnouncementRecipientClaim] = []
        """@brief 重试调用 / Retry calls."""
        self.fail_calls: list[AnnouncementRecipientClaim] = []
        """@brief 最终失败调用 / Final-failure calls."""
        self.cancel_first_mark = False
        """@brief 首次终结时模拟 kill-9 取消 / Simulate kill-9 cancellation on first finalization."""
        self.recover_calls = 0
        """@brief 租约恢复调用数 / Lease-recovery call count."""
        self.promote_calls = 0
        """@brief 投递完成推进调用数 / Delivery-completion promotion call count."""
        self.claim_calls = 0
        """@brief 公告领取调用数 / Announcement-claim call count."""

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
        self.claim_calls += 1
        batch = self.claim_batches.pop(0) if self.claim_batches else ()
        return batch[:limit]

    async def mark_expanded(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        outbound_message_id: OutboundMessageId,
        completed_at: datetime,
    ) -> bool:
        """@brief 记录终结或模拟取消 / Record finalization or simulate cancellation.

        @param claim 领取 / Claim.
        @param outbound_message_id outbox ID / Outbox ID.
        @param completed_at 终结时间 / Completion instant.
        @return True / True.
        """

        del completed_at
        if self.cancel_first_mark:
            self.cancel_first_mark = False
            raise asyncio.CancelledError
        self.mark_calls.append((claim, outbound_message_id))
        return True

    async def schedule_retry(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        retry_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 记录重试 / Record a retry.

        @param claim 领取 / Claim.
        @param retry_at 重试时间 / Retry instant.
        @param error_category 错误分类 / Error category.
        @return True / True.
        """

        del retry_at, error_category
        self.retry_calls.append(claim)
        return True

    async def mark_failed_final(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        failed_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 记录最终失败 / Record a final failure.

        @param claim 领取 / Claim.
        @param failed_at 失败时间 / Failure instant.
        @param error_category 错误分类 / Error category.
        @return True / True.
        """

        del failed_at, error_category
        self.fail_calls.append(claim)
        return True


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
    return AnnouncementRecipientClaim(
        announcement_id=AnnouncementId.for_idempotency_key("announcement:1"),
        recipient_kind=AnnouncementRecipientKind.USER,
        chat_id=42,
        message_thread_id=None,
        reply_to_message_id=None,
        body="hello",
        recipient_count=1,
        delivered_count=0,
        failed_count=0,
        claim_token=uuid4(),
        attempt_count=attempt,
        announcement_created_at=NOW,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
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
    assert operations.mark_calls[0][0].claim_token == second.claim_token


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
