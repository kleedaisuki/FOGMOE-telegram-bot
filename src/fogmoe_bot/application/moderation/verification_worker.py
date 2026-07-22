"""@brief 成员验证 durable worker / Durable member-verification worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from fogmoe_bot.application.runtime import (
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.moderation.verification import VerificationClaim

from .verification_service import VerificationRepository, VerificationService

VERIFICATION_WORKER_DATA_KEY = "fogmoe.verification_worker"
"""@brief 组合根保存验证 worker 的稳定键 / Stable composition-root key for the verification worker."""

logger = logging.getLogger(__name__)


class VerificationWorkerState(StrEnum):
    """@brief durable worker 生命周期 / Durable-worker lifecycle."""

    NEW = "new"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _StopConsumer:
    """@brief consumer 完成 drain 后的停止哨兵 / Sentinel stopping a consumer after drain."""


type _WorkItem = VerificationClaim | _StopConsumer
"""@brief verification consumer 工作项 / Verification-consumer work item."""


class VerificationTimeoutWorker:
    """@brief 单 claim producer、固定 consumers 与有界 lease 的验证 worker / Verification worker with one claim producer, fixed consumers, and bounded leases."""

    def __init__(
        self,
        *,
        repository: VerificationRepository,
        service: VerificationService,
        worker_count: int = 2,
        claim_limit: int = 1,
        lease_for: timedelta = timedelta(seconds=30),
        poll_interval: float = 0.5,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 配置固定 worker 资源边界 / Configure fixed worker resource bounds.

        @param repository claim 仓储 / Claim repository.
        @param service claim 处理器 / Claim processor.
        @param worker_count 固定并行 consumer 数 / Fixed concurrent consumer count.
        @param claim_limit 单次数据库 claim 上限 / Per-database-claim batch bound.
        @param lease_for claim 租约 / Claim lease.
        @param poll_interval 空闲轮询秒数 / Idle polling seconds.
        @param clock UTC 时钟 / UTC clock.
        @return None / None.
        """

        if worker_count <= 0 or claim_limit <= 0 or poll_interval <= 0:
            raise ValueError("verification worker bounds must be positive")
        if lease_for <= timedelta(0):
            raise ValueError("verification lease must be positive")
        self._repository = repository
        self._service = service
        self._worker_count = worker_count
        self._claim_limit = claim_limit
        self._lease_for = lease_for
        self._poll_interval = poll_interval
        self._clock = clock or SystemUtcClock()
        self._state = VerificationWorkerState.NEW

    @property
    def state(self) -> VerificationWorkerState:
        """@brief 返回 worker 生命周期 / Return worker lifecycle.

        @return 当前状态 / Current state.
        """

        return self._state

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 单点领取并以固定并发处理至停止排空 / Claim through one producer and process with fixed concurrency until drained.

        @param stop_event BotRuntime 拥有的停止信号 / Stop signal owned by BotRuntime.
        @return None / None.
        @raise RuntimeError 同一实例被重复运行时抛出 / Raised when the same instance is run more than once.
        @note 单 producer 串行化数据库 claim；容量令牌把排队中与执行中的 claim 总数
        限制为 ``worker_count * claim_limit``。正常停止先停止新 claim，再排空所有已领取
        工作。/ One producer serializes database claims; capacity tokens bound queued plus
        executing claims to ``worker_count * claim_limit``. Normal shutdown stops new claims
        before draining every acquired claim.
        """

        if self._state is not VerificationWorkerState.NEW:
            raise RuntimeError(f"verification worker cannot run from {self._state}")
        total_capacity = self._worker_count * self._claim_limit
        work_queue: asyncio.Queue[_WorkItem] = asyncio.Queue(maxsize=total_capacity)
        capacity: asyncio.Queue[None] = asyncio.Queue(maxsize=total_capacity)
        for _ in range(total_capacity):
            capacity.put_nowait(None)
        recovery = LeaseRecoveryCadence.for_lease(self._lease_for)
        try:
            recovery.take_due()
            await self._repository.recover_expired_leases(now=self._clock.now())
            self._state = VerificationWorkerState.RUNNING
            async with asyncio.TaskGroup() as task_group:
                for index in range(self._worker_count):
                    task_group.create_task(
                        self._consume(work_queue, capacity, index),
                        name=f"verification-consumer-{index}",
                    )
                task_group.create_task(
                    self._recover_leases_loop(stop_event, recovery=recovery),
                    name="verification-lease-recovery",
                )
                try:
                    await self._produce(work_queue, capacity, stop_event)
                finally:
                    self._begin_closing()
                await work_queue.join()
                for _ in range(self._worker_count):
                    await work_queue.put(_StopConsumer())
        finally:
            self._begin_closing()
            self._state = VerificationWorkerState.CLOSED

    def _begin_closing(self) -> None:
        """@brief 标记停止新 claim 并进入 drain / Stop new claims and enter drain.

        @return None / None.
        """

        if self._state is VerificationWorkerState.RUNNING:
            self._state = VerificationWorkerState.CLOSING

    async def _produce(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 以单一 owner 领取不超过可用容量的 claims / Claim up to free capacity under one owner.

        @param work_queue 有界 consumer 队列 / Bounded consumer queue.
        @param capacity 排队中与执行中共享的容量令牌 / Capacity tokens shared by queued and executing work.
        @param stop_event 停止新 claim 的顶层信号 / Top-level signal stopping new claims.
        @return None / None.
        """

        while not stop_event.is_set():
            tokens = self._take_available(capacity, limit=self._claim_limit)
            if not tokens:
                await self._wait_for_stop(stop_event, self._poll_interval)
                continue
            try:
                claims = tuple(
                    await self._repository.claim_ready(
                        now=self._clock.now(),
                        limit=len(tokens),
                        lease_for=self._lease_for,
                    )
                )
                if len(claims) > len(tokens):
                    raise RuntimeError(
                        "Verification repository returned more claims than requested"
                    )
            except asyncio.CancelledError:
                self._return_capacity(capacity, tokens)
                raise
            except Exception:
                self._return_capacity(capacity, tokens)
                logger.exception("Verification claim producer failed")
                await self._wait_for_stop(stop_event, self._poll_interval)
                continue
            for claim in claims:
                await work_queue.put(claim)
            self._return_capacity(capacity, tokens[len(claims) :])
            if claims:
                continue
            await self._wait_for_stop(stop_event, self._poll_interval)

    async def _recover_leases_loop(
        self,
        stop_event: asyncio.Event,
        *,
        recovery: LeaseRecoveryCadence,
    ) -> None:
        """@brief 周期回收崩溃任务的过期租约 / Periodically recover leases left by crashed tasks.

        @param stop_event 顶层停止信号 / Top-level stop signal.
        @param recovery 与 lease 半程对齐的共享恢复节奏 / Shared recovery cadence aligned with half the lease.
        @return None / None.
        """

        while not stop_event.is_set():
            if await self._wait_for_stop(stop_event, recovery.interval_seconds):
                return
            if not recovery.take_due():
                continue
            try:
                await self._repository.recover_expired_leases(now=self._clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Verification lease recovery failed")

    async def _consume(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        worker_index: int,
    ) -> None:
        """@brief 消费 claims 并在终结尝试后归还容量 / Consume claims and return capacity after finalization attempts.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity claim 容量令牌 / Claim-capacity tokens.
        @param worker_index 诊断序号 / Diagnostic ordinal.
        @return None / None.
        """

        while True:
            work = await work_queue.get()
            try:
                if isinstance(work, _StopConsumer):
                    return
                try:
                    await self._service.process_claim(work)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Verification claim processing failed unexpectedly: "
                        "worker=%s chat=%s user=%s",
                        worker_index,
                        work.task.chat_id,
                        work.task.user_id,
                    )
                finally:
                    capacity.put_nowait(None)
            finally:
                work_queue.task_done()

    @staticmethod
    async def _wait_for_stop(stop_event: asyncio.Event, delay: float) -> bool:
        """@brief 等待停止或有界轮询间隔 / Wait for stop or a bounded polling interval.

        @param stop_event 顶层停止信号 / Top-level stop signal.
        @param delay 最大等待秒数 / Maximum delay in seconds.
        @return 停止信号是否已置位 / Whether the stop signal is set.
        """

        try:
            async with asyncio.timeout(delay):
                await stop_event.wait()
        except TimeoutError:
            return False
        return True

    @staticmethod
    def _take_available(capacity: asyncio.Queue[None], *, limit: int) -> list[None]:
        """@brief 非阻塞取得单次 claim 的容量令牌 / Non-blockingly take one claim batch of capacity tokens.

        @param capacity 共享容量令牌 / Shared capacity tokens.
        @param limit 单次 claim 上限 / Per-claim limit.
        @return 本轮可用令牌 / Tokens available for this claim.
        """

        tokens: list[None] = []
        while len(tokens) < limit:
            try:
                tokens.append(capacity.get_nowait())
            except asyncio.QueueEmpty:
                return tokens
        return tokens

    @staticmethod
    def _return_capacity(capacity: asyncio.Queue[None], tokens: Sequence[None]) -> None:
        """@brief 归还未消费或已完成 claim 的容量 / Return unused or completed claim capacity.

        @param capacity 共享容量令牌 / Shared capacity tokens.
        @param tokens 待归还令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity.put_nowait(token)
