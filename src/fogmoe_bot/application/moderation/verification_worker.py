"""@brief 成员验证 durable worker / Durable member-verification worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from enum import StrEnum

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock

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


class VerificationTimeoutWorker:
    """@brief 固定 TaskGroup、lease/fencing 与有界 claim 的验证 worker / Verification worker with fixed TaskGroup, leases, fencing, and bounded claims."""

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
        @param worker_count 固定 worker 数 / Fixed worker count.
        @param claim_limit 每 worker 每批上限 / Per-worker batch claim bound.
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
        self._wake = asyncio.Event()

    @property
    def state(self) -> VerificationWorkerState:
        """@brief 返回 worker 生命周期 / Return worker lifecycle.

        @return 当前状态 / Current state.
        """

        return self._state

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 回收 lease 并运行固定 workers 直至停止排空 / Recover leases and run fixed workers until stopped and drained.

        @param stop_event BotRuntime 拥有的停止信号 / Stop signal owned by BotRuntime.
        @return None / None.
        @raise RuntimeError 同一实例被重复运行时抛出 / Raised when the same instance is run more than once.
        @note TaskGroup 结构化拥有所有固定 worker；正常停止先停止新 claim，
        再等待已领取批次完成。/ A TaskGroup structurally owns every fixed worker;
        normal shutdown stops new claims before awaiting already-claimed batches.
        """

        if self._state is not VerificationWorkerState.NEW:
            raise RuntimeError(f"verification worker cannot run from {self._state}")
        try:
            await self._repository.recover_expired_leases(now=self._clock.now())
            self._state = VerificationWorkerState.RUNNING
            async with asyncio.TaskGroup() as task_group:
                for index in range(self._worker_count):
                    task_group.create_task(
                        self._loop(index),
                        name=f"verification-worker-{index}",
                    )
                task_group.create_task(
                    self._recover_leases_loop(),
                    name="verification-lease-recovery",
                )
                try:
                    await stop_event.wait()
                finally:
                    self._begin_closing()
        finally:
            self._begin_closing()
            self._state = VerificationWorkerState.CLOSED

    def wake(self) -> None:
        """@brief 通知 workers 有新工作 / Notify workers of new work.

        @return None / None.
        """

        if self._state is VerificationWorkerState.RUNNING:
            self._notify_waiters()

    def _begin_closing(self) -> None:
        """@brief 停止新 claim 并唤醒固定 workers / Stop new claims and wake the fixed workers.

        @return None / None.
        """

        if self._state is VerificationWorkerState.RUNNING:
            self._state = VerificationWorkerState.CLOSING
            self._notify_waiters()

    async def _loop(self, worker_index: int) -> None:
        """@brief 固定 worker claim 循环 / Fixed worker claim loop.

        @param worker_index 诊断序号 / Diagnostic index.
        @return None / None.
        """

        while self._running():
            try:
                claims = await self._repository.claim_ready(
                    now=self._clock.now(),
                    limit=self._claim_limit,
                    lease_for=self._lease_for,
                )
                if len(claims) > self._claim_limit:
                    raise RuntimeError(
                        "Verification repository returned more claims than requested"
                    )
                if claims:
                    for claim in claims:
                        try:
                            await self._service.process_claim(claim)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception(
                                "Verification claim processing failed unexpectedly: "
                                "worker=%s chat=%s user=%s",
                                worker_index,
                                claim.task.chat_id,
                                claim.task.user_id,
                            )
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Verification claim polling failed: worker=%s",
                    worker_index,
                )
            await self._wait(self._poll_interval)

    async def _recover_leases_loop(self) -> None:
        """@brief 周期回收崩溃任务的过期租约 / Periodically recover leases left by crashed tasks.

        @return None / None.
        """

        interval = max(self._poll_interval, min(self._lease_for.total_seconds(), 5.0))
        while self._running():
            await self._wait(interval)
            if not self._running():
                return
            try:
                await self._repository.recover_expired_leases(now=self._clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Verification lease recovery failed")

    async def _wait(self, delay: float) -> None:
        """@brief 等待通知或有界轮询间隔 / Wait for a notification or a bounded poll interval.

        @param delay 最大等待秒数 / Maximum delay in seconds.
        @return None / None.
        """

        wake = self._wake
        if self._state is not VerificationWorkerState.RUNNING:
            return
        try:
            async with asyncio.timeout(delay):
                await wake.wait()
        except TimeoutError:
            pass

    def _notify_waiters(self) -> None:
        """@brief 广播一次且不丢失 shutdown 竞争 / Broadcast once without losing a shutdown race.

        @return None / None.
        """

        wake = self._wake
        self._wake = asyncio.Event()
        wake.set()

    def _running(self) -> bool:
        """@brief 判断 worker 是否仍可领取工作 / Check whether the worker may still claim work.

        @return RUNNING 状态为 True / True in RUNNING state.
        """

        return self._state is VerificationWorkerState.RUNNING
