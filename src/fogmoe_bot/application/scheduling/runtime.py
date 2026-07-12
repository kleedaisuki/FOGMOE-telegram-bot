"""@brief 单主循环调度运行时 / Single-main-loop scheduling runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, Protocol, TypeVar

from fogmoe_bot.application.scheduling.ports import MaintenanceTaskHandler
from fogmoe_bot.domain.scheduling import JobKind, ScheduleClaim


logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")


class ScheduleClaimExecutor(Protocol[PayloadT]):
    """@brief 工作循环所需的最小领取执行端口 / Minimal claim-and-execute port for the work loop."""

    async def claim_due(
        self,
        *,
        limit: int,
    ) -> Sequence[ScheduleClaim[PayloadT]]:
        """@brief 领取至多 limit 个任务 / Claim at most ``limit`` jobs.

        @param limit 最大领取数量 / Maximum number of claims.
        @return 带 fencing token 的领取凭证 / Claims carrying fencing tokens.
        """

        ...

    async def execute_claim(self, claim: ScheduleClaim[PayloadT]) -> bool:
        """@brief 执行并终结领取 / Execute and finalize a claim.

        @param claim 当前领取凭证 / Current claim.
        @return 业务处理成功时返回 True / True when business handling succeeds.
        """

        ...


@dataclass(frozen=True, slots=True)
class _ClaimWork(Generic[PayloadT]):
    """@brief 持久化任务队列项 / Persisted-claim queue item.

    @param claim 已领取的持久化任务 / Claimed persisted job.
    """

    claim: ScheduleClaim[PayloadT]


@dataclass(frozen=True, slots=True)
class _MaintenanceWork:
    """@brief 进程内维护队列项 / In-process maintenance queue item.

    @param handler 类型化维护处理器 / Typed maintenance handler.
    """

    handler: MaintenanceTaskHandler


type WorkItem[PayloadT] = _ClaimWork[PayloadT] | _MaintenanceWork
"""@brief 共享 consumer 队列的工作联合 / Work union consumed by the shared queue."""


class SchedulingWorkLoop(Generic[PayloadT]):
    """@brief 有界、结构化并发的调度循环 / Bounded structured-concurrency scheduling loop."""

    def __init__(
        self,
        *,
        dispatcher: ScheduleClaimExecutor[PayloadT],
        maintenance: Sequence[MaintenanceTaskHandler],
        poll_interval: float,
        worker_count: int,
    ) -> None:
        """@brief 创建调度工作循环 / Create the scheduling work loop.

        @param dispatcher 领取并终结持久化任务的端口 / Port that claims and finalizes persisted jobs.
        @param maintenance 周期维护处理器 / Periodic maintenance handlers.
        @param poll_interval 空闲轮询间隔秒数 / Idle polling interval in seconds.
        @param worker_count consumer 数量及总准入容量 / Consumer count and total admission capacity.
        @raise ValueError 配置非法或维护类型重复时抛出 / Raised for invalid configuration or duplicate maintenance kinds.
        """

        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        maintenance_map = {handler.task.kind: handler for handler in maintenance}
        if len(maintenance_map) != len(maintenance):
            raise ValueError("Duplicate maintenance-task handlers are not allowed")
        self._dispatcher = dispatcher
        self._maintenance = tuple(maintenance)
        self._poll_interval = poll_interval
        self._worker_count = worker_count

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行一个 producer 与多个受监督 consumer / Run one producer and supervised consumers.

        @param stop_event 请求停止时置位 / Set to request shutdown.
        @return None / None.
        @note 容量令牌从领取前持有至任务终结，已领取加运行任务不会超过 worker_count /
        Capacity is reserved before claiming and held through finalization, so claimed plus running work never exceeds worker_count.
        """

        work_queue: asyncio.Queue[WorkItem[PayloadT]] = asyncio.Queue(
            maxsize=self._worker_count
        )
        capacity_tokens: asyncio.Queue[object] = asyncio.Queue(
            maxsize=self._worker_count
        )
        for _ in range(self._worker_count):
            capacity_tokens.put_nowait(object())
        loop = asyncio.get_running_loop()
        next_maintenance_run = {
            handler.task.kind: loop.time() + handler.task.initial_delay.total_seconds()
            for handler in self._maintenance
        }

        async with asyncio.TaskGroup() as task_group:
            consumers = tuple(
                task_group.create_task(
                    self._consume(work_queue, capacity_tokens),
                    name=f"scheduling-consumer-{index}",
                )
                for index in range(self._worker_count)
            )
            producer = task_group.create_task(
                self._produce(
                    work_queue,
                    capacity_tokens,
                    next_maintenance_run,
                    stop_event,
                ),
                name="scheduling-producer",
            )
            await stop_event.wait()
            await producer
            await work_queue.join()
            for consumer in consumers:
                consumer.cancel()

    async def _produce(
        self,
        work_queue: asyncio.Queue[WorkItem[PayloadT]],
        capacity_tokens: asyncio.Queue[object],
        next_maintenance_run: dict[JobKind, float],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 领取持久化任务并投递维护任务 / Claim persisted jobs and submit maintenance work.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param capacity_tokens 总准入容量令牌 / Total-admission capacity tokens.
        @param next_maintenance_run 每类维护任务的下次时刻 / Next monotonic time per maintenance kind.
        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            tokens = self._take_available_tokens(capacity_tokens)
            if tokens:
                used_tokens = await self._submit_due_maintenance(
                    work_queue,
                    tokens,
                    next_maintenance_run,
                    now=loop.time(),
                )
                remaining_tokens = tokens[used_tokens:]
                if remaining_tokens:
                    await self._claim_persisted_work(
                        work_queue,
                        capacity_tokens,
                        remaining_tokens,
                    )
            try:
                async with asyncio.timeout(self._poll_interval):
                    await stop_event.wait()
            except TimeoutError:
                pass

    async def _submit_due_maintenance(
        self,
        work_queue: asyncio.Queue[WorkItem[PayloadT]],
        tokens: Sequence[object],
        next_maintenance_run: dict[JobKind, float],
        *,
        now: float,
    ) -> int:
        """@brief 投递到期维护任务 / Submit due maintenance tasks.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param tokens 本轮可用容量 / Capacity available in this iteration.
        @param next_maintenance_run 每类维护任务的下次时刻 / Next time per maintenance kind.
        @param now 当前单调时钟 / Current monotonic time.
        @return 已占用令牌数 / Number of occupied tokens.
        """

        submitted = 0
        for handler in self._maintenance:
            if submitted == len(tokens):
                break
            task = handler.task
            next_run = next_maintenance_run[task.kind]
            if next_run > now:
                continue
            await work_queue.put(_MaintenanceWork(handler))
            next_maintenance_run[task.kind] = self._next_maintenance_run(
                task.interval,
                next_run,
                now,
            )
            submitted += 1
        return submitted

    async def _claim_persisted_work(
        self,
        work_queue: asyncio.Queue[WorkItem[PayloadT]],
        capacity_tokens: asyncio.Queue[object],
        tokens: Sequence[object],
    ) -> None:
        """@brief 领取持久化任务并归还未用容量 / Claim persisted jobs and return unused capacity.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param capacity_tokens 总准入容量令牌 / Total-admission capacity tokens.
        @param tokens 已预留容量 / Reserved capacity.
        @return None / None.
        """

        try:
            claims = await self._dispatcher.claim_due(limit=len(tokens))
        except Exception:
            self._return_tokens(capacity_tokens, tokens)
            logger.exception("Scheduling producer failed to claim due jobs")
            return
        if len(claims) > len(tokens):
            self._return_tokens(capacity_tokens, tokens)
            raise RuntimeError(
                "Schedule dispatcher returned more claims than requested"
            )
        for claim in claims:
            await work_queue.put(_ClaimWork(claim))
        self._return_tokens(capacity_tokens, tokens[len(claims) :])

    async def _consume(
        self,
        work_queue: asyncio.Queue[WorkItem[PayloadT]],
        capacity_tokens: asyncio.Queue[object],
    ) -> None:
        """@brief 消费并终结工作项 / Consume and finalize work items.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param capacity_tokens 总准入容量令牌 / Total-admission capacity tokens.
        @return None / None.
        """

        while True:
            work = await work_queue.get()
            try:
                if isinstance(work, _ClaimWork):
                    succeeded = await self._dispatcher.execute_claim(work.claim)
                    logger.info(
                        "Scheduling claim completed: schedule_id=%s succeeded=%s",
                        work.claim.job.schedule_id,
                        succeeded,
                    )
                else:
                    await work.handler.handle()
                    logger.info(
                        "Maintenance task completed: kind=%s",
                        work.handler.task.kind.value,
                    )
            except Exception:
                logger.exception(
                    "Scheduling consumer failed while executing a work item"
                )
            finally:
                work_queue.task_done()
                capacity_tokens.put_nowait(object())

    @staticmethod
    def _next_maintenance_run(
        interval: timedelta, previous: float, now: float
    ) -> float:
        """@brief 计算严格晚于当前时刻的下次维护时间 / Compute the next maintenance time strictly after now.

        @param interval 维护周期 / Maintenance interval.
        @param previous 上次计划时刻 / Previous scheduled time.
        @param now 当前单调时刻 / Current monotonic time.
        @return 下次单调时刻 / Next monotonic time.
        """

        interval_seconds = interval.total_seconds()
        skipped = max(1, int((now - previous) // interval_seconds) + 1)
        return previous + skipped * interval_seconds

    @staticmethod
    def _take_available_tokens(
        capacity_tokens: asyncio.Queue[object],
    ) -> list[object]:
        """@brief 非阻塞取出全部空闲容量 / Take all free capacity without blocking.

        @param capacity_tokens 总准入容量令牌 / Total-admission capacity tokens.
        @return 本轮预留令牌 / Tokens reserved for this iteration.
        """

        tokens: list[object] = []
        while True:
            try:
                tokens.append(capacity_tokens.get_nowait())
            except asyncio.QueueEmpty:
                return tokens

    @staticmethod
    def _return_tokens(
        capacity_tokens: asyncio.Queue[object],
        tokens: Sequence[object],
    ) -> None:
        """@brief 归还未使用容量 / Return unused capacity.

        @param capacity_tokens 总准入容量令牌 / Total-admission capacity tokens.
        @param tokens 要归还的令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity_tokens.put_nowait(token)


__all__ = [
    "ScheduleClaimExecutor",
    "SchedulingWorkLoop",
]
