"""@brief 调度生产者—消费者运行时测试 / Scheduling producer-consumer runtime tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fogmoe_bot.application.scheduling.runtime import SchedulingWorkLoop
from fogmoe_bot.domain.scheduling import (
    JobKind,
    MaintenanceTask,
    Recurrence,
    ScheduleClaim,
    ScheduledJob,
)


def _claim(schedule_id: int) -> ScheduleClaim[Any]:
    """@brief 构造测试领取凭证 / Build a test claim.

    @param schedule_id 调度任务 ID / Schedule identifier.
    @return 测试用领取凭证 / Test claim.
    """

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ScheduleClaim(
        job=ScheduledJob(
            schedule_id=schedule_id,
            owner_id=1,
            kind=JobKind("test.job"),
            run_at=now,
            created_at=now,
            recurrence=Recurrence(),
            payload=None,
        ),
        token=f"token-{schedule_id}",
        lease_expires_at=now,
    )


class _Dispatcher:
    """@brief 可控 dispatcher 测试替身 / Controllable dispatcher test double."""

    def __init__(self, claims: tuple[ScheduleClaim[Any], ...]) -> None:
        """@brief 创建测试替身 / Create the test double.

        @param claims 首次领取时返回的任务 / Claims returned by the first claim operation.
        """

        self._claims = list(claims)
        self.requested_limits: list[int] = []
        self.executing: list[int] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()
        self.execution_loop: asyncio.AbstractEventLoop | None = None

    async def claim_due(self, *, limit: int) -> tuple[ScheduleClaim[Any], ...]:
        """@brief 按上限返回未领取任务 / Return unclaimed work under the requested limit.

        @param limit 领取上限 / Claim limit.
        @return 领取结果 / Claimed work.
        """

        self.requested_limits.append(limit)
        claims = tuple(self._claims[:limit])
        del self._claims[:limit]
        return claims

    async def execute_claim(self, claim: ScheduleClaim[Any]) -> bool:
        """@brief 阻塞执行以观察最大并发 / Block execution to observe maximum concurrency.

        @param claim 正在执行的任务 / Claim being executed.
        @return True / True.
        """

        self.executing.append(claim.job.schedule_id)
        self.execution_loop = asyncio.get_running_loop()
        if len(self.executing) == 2:
            self.all_started.set()
        await self.release.wait()
        return True


class _Maintenance:
    """@brief 可控维护任务处理器 / Controllable maintenance-task handler."""

    task = MaintenanceTask(
        kind=JobKind("maintenance.test"),
        interval=timedelta(hours=1),
    )

    def __init__(self) -> None:
        """@brief 初始化执行状态 / Initialize execution state."""

        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self) -> None:
        """@brief 阻塞执行以观察队列行为 / Block execution to observe queue behavior.

        @return None / None.
        """

        self.calls += 1
        self.started.set()
        await self.release.wait()


def test_work_loop_caps_claimed_and_running_jobs_at_worker_count() -> None:
    """@brief 容量令牌限制已领取与运行任务总数 / Capacity tokens cap total claimed and running work."""

    async def scenario() -> None:
        """@brief 运行并观察有界工作循环 / Run and observe the bounded work loop.

        @return None / None.
        """

        dispatcher = _Dispatcher((_claim(1), _claim(2), _claim(3)))
        stop_event = asyncio.Event()
        work_loop = SchedulingWorkLoop(
            dispatcher=dispatcher,
            maintenance=(),
            poll_interval=0.01,
            worker_count=2,
        )
        task = asyncio.create_task(work_loop.run(stop_event))
        await asyncio.wait_for(dispatcher.all_started.wait(), timeout=1)
        await asyncio.sleep(0.03)

        assert dispatcher.requested_limits == [2]
        assert dispatcher.executing == [1, 2]

        stop_event.set()
        dispatcher.release.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_work_loop_submits_periodic_maintenance_to_same_bounded_queue() -> None:
    """@brief 维护任务进入同一有界调度队列 / Maintenance work enters the same bounded scheduling queue."""

    async def scenario() -> None:
        """@brief 运行并观察维护任务投递 / Run and observe maintenance-task submission.

        @return None / None.
        """

        dispatcher = _Dispatcher(())
        maintenance = _Maintenance()
        stop_event = asyncio.Event()
        work_loop = SchedulingWorkLoop(
            dispatcher=dispatcher,
            maintenance=(maintenance,),
            poll_interval=0.01,
            worker_count=1,
        )
        task = asyncio.create_task(work_loop.run(stop_event))
        await asyncio.wait_for(maintenance.started.wait(), timeout=1)
        await asyncio.sleep(0.03)

        assert maintenance.calls == 1
        assert dispatcher.requested_limits == []

        stop_event.set()
        maintenance.release.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
