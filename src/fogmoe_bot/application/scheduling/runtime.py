"""@brief 同进程调度运行时 / In-process scheduling runtime."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from telegram import Bot
from telegram.request import HTTPXRequest

from fogmoe_bot.application.assistant.prompt_job_handler import PromptJobHandler
from fogmoe_bot.domain.scheduling import (
    JobKind,
    MaintenanceTaskHandler,
    ScheduleClaim,
    ScheduleDispatcher,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.repositories.schedule_repository import ScheduleRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ClaimWork:
    """@brief 持久化任务领取的队列项 / Queue item for a persisted job claim.

    @param claim 已领取的持久化调度任务 / Claimed persisted scheduled job.
    """

    claim: ScheduleClaim[Any]


@dataclass(frozen=True, slots=True)
class _MaintenanceWork:
    """@brief 进程内维护任务的队列项 / Queue item for an in-process maintenance task.

    @param handler 维护任务的类型化处理器 / Typed maintenance-task handler.
    """

    handler: MaintenanceTaskHandler


WorkItem = _ClaimWork | _MaintenanceWork
"""@brief 同一 consumer 队列可消费的工作项 / Work item consumable by the shared consumer queue."""


def create_scheduling_bot() -> Bot:
    """@brief 创建调度线程专属 Telegram 客户端 / Create the scheduling thread's Telegram client.

    @return 仅由调度 event loop 使用的 Telegram Bot / Telegram Bot used only by the scheduling event loop.
    """

    request = HTTPXRequest(
        connection_pool_size=config.SCHEDULING_CONNECTION_POOL_SIZE,
        connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=config.TELEGRAM_READ_TIMEOUT,
        write_timeout=config.TELEGRAM_WRITE_TIMEOUT,
        pool_timeout=config.TELEGRAM_POOL_TIMEOUT,
        proxy=config.NETWORK_PROXY_URL,
    )
    return Bot(token=config.TELEGRAM_BOT_TOKEN, request=request)


class SchedulingWorkLoop:
    """@brief 有界生产者—消费者调度循环 / Bounded producer-consumer scheduling loop."""

    def __init__(
        self,
        *,
        dispatcher: ScheduleDispatcher,
        maintenance: Sequence[MaintenanceTaskHandler],
        poll_interval: float,
        worker_count: int,
    ) -> None:
        """@brief 创建工作循环 / Create the work loop.

        @param dispatcher 领取与终结持久化任务的领域服务 / Domain service for persisted jobs.
        @param maintenance 周期维护任务的类型化处理器 / Typed periodic maintenance-task handlers.
        @param poll_interval 空闲 producer 轮询间隔 / Idle producer polling interval.
        @param worker_count consumer worker 数量 / Number of consumer workers.
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
        """@brief 运行一个 producer 与多个 consumer / Run one producer and multiple consumers.

        @param stop_event 请求停止时置位 / Set to request shutdown.
        @return None / None.
        @note 容量令牌从领取或投递开始持有到任务终结，已接收工作总数不会超过 worker_count /
        A capacity token is held from claim or submission through completion, so accepted work never exceeds worker_count.
        """

        work_queue: asyncio.Queue[WorkItem] = asyncio.Queue(maxsize=self._worker_count)
        capacity_tokens: asyncio.Queue[None] = asyncio.Queue(maxsize=self._worker_count)
        for _ in range(self._worker_count):
            capacity_tokens.put_nowait(None)
        next_maintenance_run = {
            handler.task.kind: asyncio.get_running_loop().time()
            + handler.task.initial_delay.total_seconds()
            for handler in self._maintenance
        }

        consumers = tuple(
            asyncio.create_task(
                self._consume(work_queue, capacity_tokens),
                name=f"scheduling-consumer-{index}",
            )
            for index in range(self._worker_count)
        )
        producer = asyncio.create_task(
            self._produce(
                work_queue,
                capacity_tokens,
                next_maintenance_run,
                stop_event,
            ),
            name="scheduling-producer",
        )
        try:
            await stop_event.wait()
        finally:
            stop_event.set()
            await asyncio.gather(producer, return_exceptions=True)
            await work_queue.join()
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    async def _produce(
        self,
        work_queue: asyncio.Queue[WorkItem],
        capacity_tokens: asyncio.Queue[None],
        next_maintenance_run: dict[JobKind, float],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 领取持久化任务并投递周期维护任务 / Claim persisted work and submit periodic maintenance work.

        @param work_queue 已接收工作的有界队列 / Bounded queue of accepted work.
        @param capacity_tokens 已接收工作总量的令牌桶 / Token bucket for total accepted work.
        @param next_maintenance_run 每类维护任务的下次运行时刻 / Next run time per maintenance-task kind.
        @param stop_event 请求停止时置位 / Set to request shutdown.
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
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue

    async def _submit_due_maintenance(
        self,
        work_queue: asyncio.Queue[WorkItem],
        tokens: Sequence[None],
        next_maintenance_run: dict[JobKind, float],
        *,
        now: float,
    ) -> int:
        """@brief 向队列投递已到期维护任务 / Submit due maintenance tasks to the queue.

        @param work_queue 已接收工作的有界队列 / Bounded queue of accepted work.
        @param tokens 可用于投递的容量令牌 / Capacity tokens available for submission.
        @param next_maintenance_run 每类维护任务的下次运行时刻 / Next run time per maintenance-task kind.
        @param now 当前单调时钟时刻 / Current monotonic-clock time.
        @return 已消耗的令牌数 / Number of consumed tokens.
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
            next_maintenance_run[task.kind] = self._next_maintenance_run(task.interval, next_run, now)
            submitted += 1
        return submitted

    async def _claim_persisted_work(
        self,
        work_queue: asyncio.Queue[WorkItem],
        capacity_tokens: asyncio.Queue[None],
        tokens: Sequence[None],
    ) -> None:
        """@brief 领取数据库任务并归还未使用容量 / Claim database jobs and return unused capacity.

        @param work_queue 已接收工作的有界队列 / Bounded queue of accepted work.
        @param capacity_tokens 已接收工作总量的令牌桶 / Token bucket for total accepted work.
        @param tokens 可用于领取的容量令牌 / Capacity tokens available for claims.
        @return None / None.
        """

        try:
            claims = await self._dispatcher.claim_due(limit=len(tokens))
        except Exception:
            self._return_tokens(capacity_tokens, tokens)
            logger.exception("Scheduling producer failed to claim due jobs")
            return
        for claim in claims:
            await work_queue.put(_ClaimWork(claim))
        self._return_tokens(capacity_tokens, tokens[len(claims) :])

    async def _consume(
        self,
        work_queue: asyncio.Queue[WorkItem],
        capacity_tokens: asyncio.Queue[None],
    ) -> None:
        """@brief 消费一个工作项并释放容量令牌 / Consume a work item and release its capacity token.

        @param work_queue 已接收工作队列 / Queue of accepted work.
        @param capacity_tokens 已接收工作总量的令牌桶 / Token bucket for total accepted work.
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
                logger.exception("Scheduling consumer crashed while executing work item")
            finally:
                work_queue.task_done()
                capacity_tokens.put_nowait(None)

    @staticmethod
    def _next_maintenance_run(interval: timedelta, previous: float, now: float) -> float:
        """@brief 计算严格晚于当前时刻的下一维护时刻 / Compute the next maintenance time strictly after now.

        @param interval 维护周期 / Maintenance interval.
        @param previous 上一次计划时刻 / Previous scheduled time.
        @param now 当前单调时钟时刻 / Current monotonic-clock time.
        @return 下一次单调时钟时刻 / Next monotonic-clock time.
        """

        interval_seconds = interval.total_seconds()
        skipped = max(1, int((now - previous) // interval_seconds) + 1)
        return previous + skipped * interval_seconds

    @staticmethod
    def _take_available_tokens(capacity_tokens: asyncio.Queue[None]) -> list[None]:
        """@brief 非阻塞地取出所有空闲容量 / Take all currently free capacity without blocking.

        @param capacity_tokens 容量令牌桶 / Capacity-token bucket.
        @return 本轮领取对应的令牌 / Tokens for this producer round.
        """

        tokens: list[None] = []
        while True:
            try:
                tokens.append(capacity_tokens.get_nowait())
            except asyncio.QueueEmpty:
                return tokens

    @staticmethod
    def _return_tokens(
        capacity_tokens: asyncio.Queue[None],
        tokens: Sequence[None],
    ) -> None:
        """@brief 归还未被任务占用的容量 / Return capacity not occupied by a work item.

        @param capacity_tokens 容量令牌桶 / Capacity-token bucket.
        @param tokens 要归还的令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity_tokens.put_nowait(token)


class SchedulingRuntime:
    """@brief 由守护线程承载的调度运行时 / Scheduling runtime hosted by a daemon thread."""

    def __init__(self) -> None:
        """@brief 创建未启动的运行时 / Create an unstarted runtime."""

        self._stop_requested = threading.Event()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread = threading.Thread(
            target=self._run_thread,
            name="scheduling-runtime",
            daemon=True,
        )

    def start(self) -> None:
        """@brief 启动调度守护线程 / Start the scheduling daemon thread.

        @return None / None.
        """

        self._thread.start()

    def stop(self) -> None:
        """@brief 请求停止并等待已接收任务终结 / Request stop and wait for accepted work to finalize.

        @return None / None.
        """

        self._stop_requested.set()
        self._ready.wait()
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join()

    def _run_thread(self) -> None:
        """@brief 守护线程入口 / Daemon-thread entry point.

        @return None / None.
        """

        try:
            asyncio.run(self._run_async())
        except Exception:
            logger.exception("Scheduling runtime terminated unexpectedly")
        finally:
            self._ready.set()

    async def _run_async(self) -> None:
        """@brief 在线程专属 event loop 中初始化并运行 / Initialize and run on the thread-owned event loop.

        @return None / None.
        """

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        db.bind_loop(self._loop)
        self._ready.set()
        if self._stop_requested.is_set():
            self._stop_event.set()
        bot = create_scheduling_bot()
        inference_executor = ThreadPoolExecutor(
            max_workers=config.SCHEDULING_WORKER_COUNT,
            thread_name_prefix="scheduled-agent",
        )
        try:
            await bot.initialize()
            from fogmoe_bot.application.scheduling.maintenance import maintenance_handlers

            dispatcher = ScheduleDispatcher(
                repository=ScheduleRepository(),
                handlers=(PromptJobHandler(bot, inference_executor=inference_executor),),
                batch_size=config.SCHEDULING_WORKER_COUNT,
                stale_after=timedelta(seconds=config.SCHEDULING_LEASE_SECONDS),
            )
            work_loop = SchedulingWorkLoop(
                dispatcher=dispatcher,
                maintenance=maintenance_handlers(),
                poll_interval=config.SCHEDULING_POLL_INTERVAL,
                worker_count=config.SCHEDULING_WORKER_COUNT,
            )
            logger.info("Scheduling runtime started")
            await work_loop.run(self._stop_event)
        finally:
            inference_executor.shutdown(wait=True)
            try:
                await bot.shutdown()
            finally:
                await db.dispose_current_engine()
                logger.info("Scheduling runtime stopped")
