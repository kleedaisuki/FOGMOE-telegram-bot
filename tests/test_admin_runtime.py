"""@brief AdminRuntime 崩溃重放与资源边界测试 / AdminRuntime crash-replay and resource-bound tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fogmoe_bot.application.admin.runtime import AdminRuntime
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

    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        """@brief 模拟无过期租约 / Simulate no expired leases.

        @param now 当前时间 / Current instant.
        @param limit 批量上限 / Batch limit.
        @return 零 / Zero.
        """

        del now, limit
        return 0

    async def promote_delivery_completions(self, *, now: datetime, limit: int) -> int:
        """@brief 模拟无完成推进 / Simulate no completion promotion.

        @param now 当前时间 / Current instant.
        @param limit 批量上限 / Batch limit.
        @return 零 / Zero.
        """

        del now, limit
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


def test_runtime_constructor_rejects_unbounded_configuration() -> None:
    """@brief worker 拒绝无界或非法资源配置 / Worker rejects unbounded or invalid resource configuration."""

    with pytest.raises(ValueError, match="positive"):
        AdminRuntime(
            operations=ScriptedOperations([]),
            outbound=RecordingOutbound(),
            factory=TelegramAnnouncementOutboundFactory(),
            batch_size=0,
        )
