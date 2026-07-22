"""@brief 可恢复、有界的 Admin 公告 worker / Recoverable bounded worker for administrative announcements."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.application.runtime import (
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.admin import AnnouncementRecipientClaim
from fogmoe_bot.domain.conversation.identity import OutboundMessageId

from .ports import AdminAnnouncementOperations, AnnouncementOutboundFactory

logger = logging.getLogger(__name__)


class AdminRuntime:
    """@brief 领取公告回执并幂等扩展到 transactional outbox / Claim announcement receipts and idempotently expand them into the transactional outbox."""

    def __init__(
        self,
        *,
        operations: AdminAnnouncementOperations,
        outbound: StandaloneOutboundCapability,
        factory: AnnouncementOutboundFactory,
        clock: UtcClock | None = None,
        batch_size: int = 32,
        poll_interval: float = 1.0,
        lease_for: timedelta = timedelta(minutes=2),
        max_attempts: int = 8,
        initial_retry: timedelta = timedelta(seconds=1),
        max_retry: timedelta = timedelta(minutes=5),
    ) -> None:
        """@brief 注入持久化、outbox 与所有资源边界 / Inject persistence, outbox, and every resource bound.

        @param operations 公告回执端口 / Announcement-receipt port.
        @param outbound 共享 standalone outbox 能力 / Shared standalone-outbox capability.
        @param factory connector 出站工厂 / Connector outbound factory.
        @param clock 可测试 UTC 时钟 / Testable UTC clock.
        @param batch_size 单轮最大回执数 / Maximum receipts per pass.
        @param poll_interval 空闲轮询间隔 / Idle polling interval.
        @param lease_for 领取租约 / Claim lease duration.
        @param max_attempts 最大尝试数 / Maximum attempt count.
        @param initial_retry 首次重试延迟 / Initial retry delay.
        @param max_retry 重试延迟上限 / Retry-delay cap.
        @raise ValueError 资源边界非法 / Resource bounds are invalid.
        """

        if batch_size < 1 or poll_interval <= 0 or max_attempts < 1:
            raise ValueError("Admin runtime count and interval bounds must be positive")
        if (
            lease_for <= timedelta(0)
            or initial_retry <= timedelta(0)
            or max_retry < initial_retry
        ):
            raise ValueError("Admin runtime duration bounds are invalid")
        self._operations = operations
        self._outbound = outbound
        self._factory = factory
        self._clock = clock or SystemUtcClock()
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._lease_for = lease_for
        """@brief kill-9 恢复租约 / Kill-9 recovery lease."""
        self._max_attempts = max_attempts
        self._initial_retry = initial_retry
        self._max_retry = max_retry

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 在结构化生命周期内运行 / Run within the structured service lifecycle.

        @param stop_event 统一停止事件 / Unified stop event.
        @return None / None.
        @note CancelledError 不会被吞掉 / CancelledError is never swallowed.
        """

        recovery = LeaseRecoveryCadence.for_lease(self._lease_for)
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(
                self._recover_leases(stop_event, recovery=recovery),
                name="admin-announcement-recovery",
            )
            task_group.create_task(
                self._run_business_passes(stop_event),
                name="admin-announcement-business",
            )

    async def _run_business_passes(self, stop_event: asyncio.Event) -> None:
        """@brief 按固定 SLO 推进投递完成并扩展公告 / Promote delivery completions and expand announcements at the fixed SLO.

        @param stop_event 统一停止事件 / Unified stop event.
        @return None / None.
        """

        while not stop_event.is_set():
            try:
                work = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Admin announcement runtime pass failed")
                work = 0
            if work == 0:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._poll_interval
                    )
                except TimeoutError:
                    pass

    async def _recover_leases(
        self,
        stop_event: asyncio.Event,
        *,
        recovery: LeaseRecoveryCadence,
    ) -> None:
        """@brief 以单一 owner 独立低频回收过期公告租约 / Recover expired announcement leases independently under one owner.

        @param stop_event 统一停止事件 / Unified stop event.
        @param recovery 与租约半程对齐的恢复节奏 / Recovery cadence aligned with half the lease.
        @return None；恢复故障不会取消业务轮询 / None; recovery failures do not cancel business polling.
        @note 首轮立即恢复，后续周期不受公告轮询阻塞或空闲等待影响 / The first
            pass recovers immediately; subsequent periods are independent of announcement
            polling stalls and idle waits.
        """

        while not stop_event.is_set():
            if recovery.take_due():
                await self._recover_expired_leases()
            try:
                async with asyncio.timeout(recovery.interval_seconds):
                    await stop_event.wait()
            except TimeoutError:
                continue

    async def _recover_expired_leases(self) -> None:
        """@brief 回收一个有界批次的过期公告租约 / Recover one bounded batch of expired announcement leases.

        @return None；持久化故障隔离到后续 cadence / None; persistence failures are isolated until a later cadence.
        """

        try:
            recovered = await self._operations.recover_expired(
                now=self._clock.now(),
                limit=self._batch_size,
            )
            if recovered:
                logger.warning(
                    "Recovered expired admin announcement leases: count=%s",
                    recovered,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Admin announcement lease recovery failed; a later pass will retry"
            )

    async def run_once(self) -> int:
        """@brief 推进投递终态并处理一个有界公告批次 / Promote delivery terminals and process one bounded announcement batch.

        @return 本轮完成的状态转移数 / Number of state transitions completed in this pass.
        @note 租约恢复由 ``run`` 中的独立结构化任务唯一持有 / Lease recovery is owned solely by the independent structured task in ``run``.
        """

        now = self._clock.now()
        work = await self._operations.promote_delivery_completions(
            now=now,
            limit=self._batch_size,
        )
        claims = await self._operations.claim_ready(
            now=now,
            lease_for=self._lease_for,
            limit=self._batch_size,
        )
        for claim in claims:
            await self._process_claim(claim)
            work += 1
        return work

    async def _process_claim(self, claim: AnnouncementRecipientClaim) -> None:
        """@brief 幂等写 outbox 后以 fencing token 终结回执 / Idempotently write the outbox and then finalize the receipt with its fencing token.

        @param claim 已领取回执 / Claimed receipt.
        @return None / None.
        @note outbox 插入后崩溃会重放同一确定性消息，不会产生第二条语义副作用 / A crash after outbox insertion replays the same deterministic message rather than creating a second semantic effect.
        """

        try:
            command = self._factory.build(claim)
            await self._outbound.enqueue(command)
            message_id = OutboundMessageId.for_conversation(
                command.conversation_id,
                command.idempotency_key,
            )
            await self._operations.mark_expanded(
                claim,
                outbound_message_id=message_id,
                completed_at=self._clock.now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "Admin announcement receipt expansion failed: announcement=%s kind=%s chat=%s attempt=%s",
                claim.announcement_id,
                claim.recipient_kind.value,
                claim.chat_id,
                claim.attempt_count,
            )
            category = type(error).__name__[:100] or "unknown"
            now = self._clock.now()
            if claim.attempt_count >= self._max_attempts:
                await self._operations.mark_failed_final(
                    claim,
                    failed_at=now,
                    error_category=category,
                )
                return
            await self._operations.schedule_retry(
                claim,
                retry_at=now + self._retry_delay(claim.attempt_count),
                error_category=category,
            )

    def _retry_delay(self, attempt_count: int) -> timedelta:
        """@brief 计算 capped exponential backoff / Calculate capped exponential backoff.

        @param attempt_count 已开始的尝试数 / Number of attempts already started.
        @return 下次延迟 / Next delay.
        """

        multiplier = 2 ** max(0, attempt_count - 1)
        candidate_seconds = self._initial_retry.total_seconds() * multiplier
        return timedelta(
            seconds=min(candidate_seconds, self._max_retry.total_seconds())
        )


__all__ = ["AdminRuntime"]
