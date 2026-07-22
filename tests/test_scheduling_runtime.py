"""@brief 专用 Scheduled Assistant worker 测试 / Dedicated Scheduled Assistant worker tests."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fogmoe_bot.application.assistant.inference_command import DurableAssistantUser
from fogmoe_bot.application.conversation.workflow import PreparedTurnAcceptance
from fogmoe_bot.application.scheduling.worker import ScheduleWorker
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    FixedInterval,
    MisfirePolicy,
    ScheduleClaim,
    ScheduledAssistantTurn,
    ScheduleTarget,
    StaleScheduleClaimError,
)
from fogmoe_bot.domain.temporal import TimeZoneId

RUN_AT = datetime(2026, 7, 22, 12, tzinfo=UTC)
"""@brief 当前未消费 occurrence / Current unconsumed occurrence."""

NOW = RUN_AT + timedelta(minutes=30)
"""@brief worker 固定观察时刻 / Fixed worker observation instant."""


class _FixedClock:
    """@brief 返回固定时刻的 worker 时钟 / Worker clock returning a fixed instant."""

    def now(self) -> datetime:
        """@brief 返回 NOW / Return NOW.

        @return 固定 UTC 时刻 / Fixed UTC instant.
        """

        return NOW


class _MonotonicClock:
    """@brief 可手动推进的单调时钟 / Manually advanced monotonic clock."""

    def __init__(self) -> None:
        """@brief 从零创建时钟 / Create a clock starting at zero."""

        self.seconds = 0.0

    def __call__(self) -> float:
        """@brief 返回当前单调秒数 / Return the current monotonic seconds.

        @return 当前秒数 / Current seconds.
        """

        return self.seconds

    def advance(self, seconds: float) -> None:
        """@brief 推进单调时钟 / Advance the monotonic clock.

        @param seconds 正向增量 / Positive increment.
        @return None / None.
        """

        self.seconds += seconds


def _user() -> DurableAssistantUser:
    """@brief 构造 acceptance-time 用户 / Build an acceptance-time user.

    @return 测试用户 / Test user.
    """

    return DurableAssistantUser(
        user_id=42,
        username="klee",
        display_name="Klee",
        coins=0,
        plan=AccountPlan.FREE,
        permission=0,
        profile=None,
        personal_info="",
        diary_exists=False,
    )


def _schedule(
    schedule_id: int,
    *,
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE,
    misfire_grace: timedelta | None = None,
) -> ScheduledAssistantTurn:
    """@brief 构造到期的 fixed-interval schedule / Build a due fixed-interval schedule.

    @param schedule_id 计划 ID / Schedule ID.
    @param misfire_policy 过期策略 / Misfire policy.
    @param misfire_grace 可选宽限 / Optional grace window.
    @return 到期聚合 / Due aggregate.
    """

    return ScheduledAssistantTurn(
        schedule_id=schedule_id,
        creator_user_id=42,
        target=ScheduleTarget(
            conversation_id=ConversationId("assistant-user:42"),
            delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
            chat_id=42,
            is_group=False,
        ),
        trigger_reason="timer",
        instruction=f"Run scheduled task {schedule_id}",
        cadence=FixedInterval(timedelta(hours=1)),
        next_run_at=RUN_AT,
        created_at=RUN_AT - timedelta(days=1),
        time_zone=TimeZoneId("Asia/Shanghai"),
        misfire_policy=misfire_policy,
        misfire_grace=misfire_grace,
    )


def _claim(schedule_id: int, *, attempt_count: int = 1) -> ScheduleClaim:
    """@brief 构造带 fencing token 的 claim / Build a claim carrying a fencing token.

    @param schedule_id 计划 ID / Schedule ID.
    @param attempt_count 当前尝试序号 / Current attempt ordinal.
    @return 领取凭证 / Claim.
    """

    return _claim_for(_schedule(schedule_id), attempt_count=attempt_count)


def _claim_for(
    schedule: ScheduledAssistantTurn,
    *,
    attempt_count: int = 1,
) -> ScheduleClaim:
    """@brief 为特定 schedule 构造 claim / Build a claim for a specific schedule.

    @param schedule 到期计划 / Due schedule.
    @param attempt_count 当前尝试序号 / Current attempt ordinal.
    @return 领取凭证 / Claim.
    """

    return ScheduleClaim(
        schedule=schedule,
        attempt_count=attempt_count,
        token=UUID(int=schedule.schedule_id),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
    )


class _Queue:
    """@brief 可控的 fenced queue 替身 / Controllable fenced-queue double."""

    def __init__(
        self,
        claims: tuple[ScheduleClaim, ...] = (),
        *,
        recovered: int = 0,
    ) -> None:
        """@brief 注入 claims 与回收数 / Inject claims and recovery count.

        @param claims 待领取工作 / Work to claim.
        @param recovered 过期 lease 回收数 / Number of expired leases recovered.
        """

        self._claims = list(claims)
        self.recovered = recovered
        self.recover_calls: list[datetime] = []
        self.claim_calls: list[tuple[datetime, int, timedelta]] = []
        self.retries: list[tuple[ScheduleClaim, datetime, datetime, str]] = []
        self.final_failures: list[tuple[ScheduleClaim, datetime, str]] = []
        self.skips: list[tuple[ScheduleClaim, datetime | None, datetime]] = []

    async def recover_expired(self, *, now: datetime) -> int:
        """@brief 记录 lease 回收 / Record lease recovery."""

        self.recover_calls.append(now)
        return self.recovered

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[ScheduleClaim, ...]:
        """@brief 按 batch 上限领取 / Claim up to the batch limit."""

        self.claim_calls.append((now, limit, lease_for))
        claimed = tuple(self._claims[:limit])
        del self._claims[:limit]
        return claimed

    async def retry(
        self,
        claim: ScheduleClaim,
        *,
        retry_at: datetime,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录 fenced retry / Record a fenced retry."""

        self.retries.append((claim, retry_at, failed_at, error))

    async def fail_final(
        self,
        claim: ScheduleClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录最终失败 / Record a final failure."""

        self.final_failures.append((claim, failed_at, error))

    async def skip_misfire(
        self,
        claim: ScheduleClaim,
        *,
        next_run_at: datetime | None,
        skipped_at: datetime,
    ) -> None:
        """@brief 记录 misfire 跳过 / Record a misfire skip."""

        self.skips.append((claim, next_run_at, skipped_at))


class _Profiles:
    """@brief 可控的创建者快照读取端口 / Controllable creator-snapshot reader."""

    def __init__(self, profile: DurableAssistantUser | None) -> None:
        """@brief 注入读取结果 / Inject the read result."""

        self.profile = profile
        self.user_ids: list[int] = []

    async def read(self, user_id: int) -> DurableAssistantUser | None:
        """@brief 记录并返回用户 / Record and return the user."""

        self.user_ids.append(user_id)
        return self.profile


class _Acceptance:
    """@brief 可控的 schedule-conversation 原子 UoW / Controllable atomic schedule-conversation UoW."""

    def __init__(self, error: Exception | None = None) -> None:
        """@brief 注入接受失败 / Inject an acceptance failure.

        @param error 可选失败 / Optional failure.
        """

        self.error = error
        self.calls: list[
            tuple[ScheduleClaim, PreparedTurnAcceptance, datetime | None, datetime]
        ] = []

    async def accept(
        self,
        claim: ScheduleClaim,
        prepared: PreparedTurnAcceptance,
        *,
        next_run_at: datetime | None,
        accepted_at: datetime,
    ) -> None:
        """@brief 记录联合提交边界并可选失败 / Record the joint commit boundary and optionally fail."""

        self.calls.append((claim, prepared, next_run_at, accepted_at))
        if self.error is not None:
            raise self.error


def _worker(
    *,
    queue: _Queue,
    acceptance: object,
    profiles: _Profiles,
    worker_count: int = 1,
    max_attempts: int = 3,
    jitter: Callable[[float, float], float] | None = None,
    poll_interval: float = 0.01,
    lease_for: timedelta = timedelta(minutes=1),
    attempt_timeout: timedelta = timedelta(seconds=10),
    recovery_monotonic: Callable[[], float] | None = None,
) -> ScheduleWorker:
    """@brief 构造固定配置的 worker / Build a worker with fixed configuration.

    @param queue fenced queue / Fenced queue.
    @param acceptance 联合 acceptance UoW / Joint acceptance UoW.
    @param profiles 用户快照读取器 / User-snapshot reader.
    @param worker_count batch 并发上限 / Batch concurrency cap.
    @param max_attempts 最大尝试数 / Maximum attempts.
    @param jitter 可选可测 full-jitter 函数 / Optional testable full-jitter function.
    @param poll_interval 空闲 claim 间隔 / Idle claim interval.
    @param lease_for claim lease 时长 / Claim lease duration.
    @param attempt_timeout 单次执行预算 / Per-attempt execution budget.
    @param recovery_monotonic 可选回收单调时钟 / Optional recovery monotonic clock.
    @return 已配置 worker / Configured worker.
    """

    values: dict[str, object] = {}
    if jitter is not None:
        values["jitter"] = jitter
    if recovery_monotonic is not None:
        values["recovery_monotonic"] = recovery_monotonic
    return ScheduleWorker(
        queue=queue,
        acceptance=acceptance,  # type: ignore[arg-type]
        profiles=profiles,
        worker_count=worker_count,
        poll_interval=poll_interval,
        lease_for=lease_for,
        attempt_timeout=attempt_timeout,
        max_attempts=max_attempts,
        retry_base=2.0,
        retry_cap=20.0,
        clock=_FixedClock(),
        **values,  # type: ignore[arg-type]
    )


def test_worker_claims_and_atomically_accepts_before_advancing_cursor() -> None:
    """@brief claim 与 prepared Turn 一起交给原子 UoW / The claim and prepared Turn reach the atomic UoW together."""

    async def scenario() -> None:
        """@brief 执行一个成功 occurrence / Execute one successful occurrence."""

        claim = _claim(1)
        queue = _Queue((claim,))
        profiles = _Profiles(_user())
        acceptance = _Acceptance()
        handled = await _worker(
            queue=queue,
            acceptance=acceptance,
            profiles=profiles,
        ).process_once()

        assert handled == 1
        assert queue.recover_calls == []
        assert queue.claim_calls == [(NOW, 1, timedelta(minutes=1))]
        assert profiles.user_ids == [42]
        assert len(acceptance.calls) == 1
        accepted_claim, prepared, next_run_at, accepted_at = acceptance.calls[0]
        assert accepted_claim is claim
        assert prepared.turn.source.key.startswith("1:")
        assert next_run_at == datetime(2026, 7, 22, 13, tzinfo=UTC)
        assert accepted_at == NOW
        assert queue.retries == []
        assert queue.final_failures == []
        assert queue.skips == []

    asyncio.run(scenario())


def test_worker_skips_late_occurrence_without_reading_profile_or_creating_turn() -> (
    None
):
    """@brief SKIP misfire 直接推进 cursor 而不读用户或创建 Turn / A SKIP misfire advances directly without reading a user or creating a Turn."""

    async def scenario() -> None:
        """@brief 执行超出 grace 的 occurrence / Execute an occurrence outside its grace window."""

        schedule = _schedule(
            2,
            misfire_policy=MisfirePolicy.SKIP,
            misfire_grace=timedelta(minutes=5),
        )
        claim = _claim_for(schedule)
        queue = _Queue((claim,))
        profiles = _Profiles(_user())
        acceptance = _Acceptance()

        assert (
            await _worker(
                queue=queue,
                acceptance=acceptance,
                profiles=profiles,
            ).process_once()
            == 1
        )
        assert queue.skips == [(claim, datetime(2026, 7, 22, 13, tzinfo=UTC), NOW)]
        assert profiles.user_ids == []
        assert acceptance.calls == []

    asyncio.run(scenario())


def test_missing_creator_is_a_final_failure() -> None:
    """@brief 不存在的创建者是不可重试失败 / A missing creator is an unrecoverable failure."""

    async def scenario() -> None:
        """@brief 执行用户不存在的 occurrence / Execute an occurrence whose user is absent."""

        claim = _claim(3)
        queue = _Queue((claim,))
        acceptance = _Acceptance()
        await _worker(
            queue=queue,
            acceptance=acceptance,
            profiles=_Profiles(None),
        ).process_once()

        assert len(queue.final_failures) == 1
        failed_claim, failed_at, error = queue.final_failures[0]
        assert failed_claim is claim
        assert failed_at == NOW
        assert "creator not found: 42" in error
        assert queue.retries == []
        assert acceptance.calls == []

    asyncio.run(scenario())


def test_transient_failure_uses_bounded_full_jitter_retry() -> None:
    """@brief 瞬态失败使用当前 attempt 的有界 full jitter / A transient failure uses bounded full jitter for the current attempt."""

    async def scenario() -> None:
        """@brief 注入可重试 acceptance 失败 / Inject a retryable acceptance failure."""

        jitter_calls: list[tuple[float, float]] = []

        def jitter(lower: float, upper: float) -> float:
            """@brief 记录 jitter 边界并返回 75% / Record jitter bounds and return 75%."""

            jitter_calls.append((lower, upper))
            return upper * 0.75

        claim = _claim(4, attempt_count=1)
        queue = _Queue((claim,))
        await _worker(
            queue=queue,
            acceptance=_Acceptance(RuntimeError("database unavailable")),
            profiles=_Profiles(_user()),
            jitter=jitter,
        ).process_once()

        assert jitter_calls == [(0.0, 2.0)]
        assert queue.retries == [
            (
                claim,
                NOW + timedelta(seconds=1.5),
                NOW,
                "database unavailable",
            )
        ]
        assert queue.final_failures == []

    asyncio.run(scenario())


def test_max_attempts_exhaustion_is_a_final_failure() -> None:
    """@brief 耗尽尝试数后不再计算 retry / Exhausting attempts finalizes without calculating another retry."""

    async def scenario() -> None:
        """@brief 在最后一次尝试注入瞬态失败 / Inject a transient failure on the last attempt."""

        jitter_calls: list[tuple[float, float]] = []

        def jitter(lower: float, upper: float) -> float:
            """@brief 记录不应发生的 jitter 调用 / Record a jitter call that should not occur."""

            jitter_calls.append((lower, upper))
            return upper

        claim = _claim(5, attempt_count=3)
        queue = _Queue((claim,))
        await _worker(
            queue=queue,
            acceptance=_Acceptance(RuntimeError("still unavailable")),
            profiles=_Profiles(_user()),
            max_attempts=3,
            jitter=jitter,
        ).process_once()

        assert jitter_calls == []
        assert queue.retries == []
        assert queue.final_failures == [(claim, NOW, "still unavailable")]

    asyncio.run(scenario())


def test_recovered_claim_beyond_attempt_budget_fails_without_business_work() -> None:
    """@brief lease 崩溃耗尽预算后只做 fenced 终结 / A lease crash beyond the budget performs only fenced finalization."""

    async def scenario() -> None:
        """@brief 领取超预算 claim / Claim an item beyond its attempt budget."""

        claim = _claim(51, attempt_count=4)
        queue = _Queue((claim,))
        profiles = _Profiles(_user())
        acceptance = _Acceptance()
        await _worker(
            queue=queue,
            acceptance=acceptance,
            profiles=profiles,
            max_attempts=3,
        ).process_once()

        assert profiles.user_ids == []
        assert acceptance.calls == []
        assert queue.retries == []
        assert queue.final_failures == [
            (
                claim,
                NOW,
                "schedule attempt budget was exhausted while recovering a lease",
            )
        ]

    asyncio.run(scenario())


def test_stale_acceptance_token_is_swallowed_without_followup_write() -> None:
    """@brief 原子 UoW 拒绝陈旧 token 后 worker 不再写队列 / The worker swallows a stale atomic-UoW token without another queue write."""

    async def scenario() -> None:
        """@brief 模拟 claim 在提交前被回收 / Simulate a claim recovered before commit."""

        claim = _claim(6)
        queue = _Queue((claim,))
        acceptance = _Acceptance(StaleScheduleClaimError("stale token"))

        assert (
            await _worker(
                queue=queue,
                acceptance=acceptance,
                profiles=_Profiles(_user()),
            ).process_once()
            == 1
        )
        assert len(acceptance.calls) == 1
        assert queue.retries == []
        assert queue.final_failures == []
        assert queue.skips == []

    asyncio.run(scenario())


class _FailOnceRecoveryQueue(_Queue):
    """@brief 首次恢复失败、第二次成功的队列 / Queue failing its first recovery and succeeding on its second."""

    def __init__(self) -> None:
        """@brief 创建恢复同步事件 / Create recovery synchronization events."""

        super().__init__(recovered=2)
        self.first_recovery = asyncio.Event()
        self.second_recovery = asyncio.Event()

    async def recover_expired(self, *, now: datetime) -> int:
        """@brief 记录并执行可重试恢复 / Record and execute a retryable recovery.

        @param now 当前 UTC 时刻 / Current UTC instant.
        @return 第二次及以后返回恢复数 / Recovery count on the second and later calls.
        @raise RuntimeError 首次调用模拟存储故障 / First call simulates a storage failure.
        """

        self.recover_calls.append(now)
        if len(self.recover_calls) == 1:
            self.first_recovery.set()
            raise RuntimeError("recovery unavailable")
        self.second_recovery.set()
        return self.recovered


class _BlockingRecoveryQueue(_Queue):
    """@brief 阻塞恢复以观察结构化并发 / Block recovery to observe structured concurrency."""

    def __init__(self, claims: tuple[ScheduleClaim, ...] = ()) -> None:
        """@brief 创建带恢复 barrier 的队列 / Create a queue with a recovery barrier.

        @param claims 可领取计划 / Claimable schedules.
        """

        super().__init__(claims)
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()
        self.recovery_cancelled = False

    async def recover_expired(self, *, now: datetime) -> int:
        """@brief 阻塞到释放或取消 / Block until released or cancelled.

        @param now 当前 UTC 时刻 / Current UTC instant.
        @return 零恢复数 / Zero recoveries.
        """

        self.recover_calls.append(now)
        self.recovery_started.set()
        try:
            await self.release_recovery.wait()
        except asyncio.CancelledError:
            self.recovery_cancelled = True
            raise
        return 0


def test_process_once_only_claims_and_never_runs_lease_recovery() -> None:
    """@brief focused batch 边界不再执行 lease recovery / The focused batch boundary no longer performs lease recovery."""

    async def scenario() -> None:
        """@brief 执行空 batch / Execute an empty batch."""

        queue = _Queue(recovered=2)
        handled = await _worker(
            queue=queue,
            acceptance=_Acceptance(),
            profiles=_Profiles(_user()),
            worker_count=4,
        ).process_once()

        assert handled == 0
        assert queue.recover_calls == []
        assert queue.claim_calls == [(NOW, 4, timedelta(minutes=1))]

    asyncio.run(scenario())


def test_run_recovers_immediately_then_retries_on_lease_cadence() -> None:
    """@brief recovery 首次立即且失败后只在 lease cadence 重试 / Recovery is immediate and retries only on the lease cadence."""

    async def scenario() -> None:
        """@brief 推进单调时钟跨过半 lease 边界 / Advance monotonic time across the half-lease boundary."""

        monotonic = _MonotonicClock()
        queue = _FailOnceRecoveryQueue()
        stop_event = asyncio.Event()
        worker = _worker(
            queue=queue,
            acceptance=_Acceptance(),
            profiles=_Profiles(_user()),
            lease_for=timedelta(milliseconds=40),
            attempt_timeout=timedelta(milliseconds=10),
            recovery_monotonic=monotonic,
        )

        running = asyncio.create_task(worker.run(stop_event))
        await asyncio.wait_for(queue.first_recovery.wait(), timeout=1)
        monotonic.advance(0.019)
        await asyncio.sleep(0.025)
        assert queue.recover_calls == [NOW]

        monotonic.advance(0.001)
        await asyncio.wait_for(queue.second_recovery.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(running, timeout=1)

        assert queue.recover_calls == [NOW, NOW]

    asyncio.run(scenario())


def test_blocked_recovery_does_not_delay_claims_and_graceful_stop_drains_it() -> None:
    """@brief 阻塞的 recovery 不延迟 claim，graceful stop 等待结构化子任务 / Blocked recovery does not delay claims, and graceful stop drains the structured child."""

    async def scenario() -> None:
        """@brief 并行观察 claim 与恢复 barrier / Observe claims alongside the recovery barrier."""

        claim = _claim(52)
        queue = _BlockingRecoveryQueue((claim,))
        acceptance = _Acceptance()
        stop_event = asyncio.Event()
        running = asyncio.create_task(
            _worker(
                queue=queue,
                acceptance=acceptance,
                profiles=_Profiles(_user()),
            ).run(stop_event)
        )

        await asyncio.wait_for(queue.recovery_started.wait(), timeout=1)
        while not acceptance.calls:
            await asyncio.sleep(0)
        assert acceptance.calls[0][0] is claim

        stop_event.set()
        await asyncio.sleep(0)
        assert not running.done()
        queue.release_recovery.set()
        await asyncio.wait_for(running, timeout=1)
        assert not queue.recovery_cancelled
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name() in {"schedule-claims", "schedule-lease-recovery"}
        }

    asyncio.run(scenario())


def test_cancelling_run_cancels_recovery_without_detached_tasks() -> None:
    """@brief 取消顶层 run 会取消 recovery 且不遗留 detached task / Cancelling run cancels recovery without leaving detached tasks."""

    async def scenario() -> None:
        """@brief 在恢复阻塞时取消 TaskGroup owner / Cancel the TaskGroup owner while recovery is blocked."""

        queue = _BlockingRecoveryQueue()
        stop_event = asyncio.Event()
        running = asyncio.create_task(
            _worker(
                queue=queue,
                acceptance=_Acceptance(),
                profiles=_Profiles(_user()),
            ).run(stop_event)
        )
        await asyncio.wait_for(queue.recovery_started.wait(), timeout=1)

        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("ScheduleWorker.run() swallowed cancellation")

        assert queue.recovery_cancelled
        assert not {
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name() in {"schedule-claims", "schedule-lease-recovery"}
        }

    asyncio.run(scenario())


class _BlockingAcceptance:
    """@brief 用 barrier 观察 TaskGroup 并发度 / Atomic-acceptance double observing TaskGroup concurrency with a barrier."""

    def __init__(self, expected: int) -> None:
        """@brief 设置 barrier 参与者数 / Configure the barrier participant count.

        @param expected 预期同时进入数 / Expected simultaneous entries.
        """

        self.expected = expected
        self.active = 0
        self.maximum_active = 0
        self.calls: list[int] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def accept(
        self,
        claim: ScheduleClaim,
        prepared: PreparedTurnAcceptance,
        *,
        next_run_at: datetime | None,
        accepted_at: datetime,
    ) -> None:
        """@brief 阻塞直到测试释放 batch / Block until the test releases the batch."""

        del prepared, next_run_at, accepted_at
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.calls.append(claim.schedule.schedule_id)
        if self.active == self.expected:
            self.all_started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1


def test_worker_executes_claim_batch_concurrently_with_worker_bound() -> None:
    """@brief batch 的 claim 在 worker_count 边界内并发执行 / A claim batch executes concurrently within the worker-count bound."""

    async def scenario() -> None:
        """@brief 用 barrier 观察两个同时的 atomic acceptances / Observe two simultaneous atomic acceptances with a barrier."""

        queue = _Queue((_claim(7), _claim(8), _claim(9)))
        acceptance = _BlockingAcceptance(expected=2)
        worker = _worker(
            queue=queue,
            acceptance=acceptance,
            profiles=_Profiles(_user()),
            worker_count=2,
        )

        batch = asyncio.create_task(worker.process_once())
        await asyncio.wait_for(acceptance.all_started.wait(), timeout=1)
        assert acceptance.maximum_active == 2
        assert set(acceptance.calls) == {7, 8}
        acceptance.release.set()
        assert await asyncio.wait_for(batch, timeout=1) == 2
        assert queue.claim_calls == [(NOW, 2, timedelta(minutes=1))]

    asyncio.run(scenario())
