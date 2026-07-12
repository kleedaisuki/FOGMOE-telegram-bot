"""@brief durable verification service 与 worker 测试 / Tests for the durable verification service and worker."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.application.moderation.verification_service import (
    VerificationRejected,
    VerificationRejectionCode,
    VerificationService,
)
from fogmoe_bot.application.moderation.verification_worker import (
    VerificationTimeoutWorker,
    VerificationWorkerState,
)
from fogmoe_bot.domain.moderation.models import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    StaleVerificationVersion,
    VerificationClaim,
    VerificationEvent,
    VerificationFencingError,
    VerificationKey,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试起始时刻 / Fixed test start instant."""

KEY = VerificationKey(ChatId(-1001), UserId(42))
"""@brief 固定测试聚合键 / Fixed test aggregate key."""


class ManualClock:
    """@brief 可推进 UTC 测试时钟 / Advanceable UTC test clock."""

    def __init__(self, now: datetime = NOW) -> None:
        """@brief 初始化时钟 / Initialize the clock.

        @param now 初始时刻 / Initial instant.
        @return None / None.
        """

        self.current = now
        """@brief 当前时刻 / Current instant."""

    def now(self) -> datetime:
        """@brief 返回当前时刻 / Return current instant.

        @return 当前 UTC 时间 / Current UTC time.
        """

        return self.current

    def advance(self, duration: timedelta) -> None:
        """@brief 推进时钟 / Advance the clock.

        @param duration 推进时长 / Duration to advance.
        @return None / None.
        """

        self.current += duration


class MemoryVerificationRepository:
    """@brief 具有 OCC、lease 与 fencing 的内存测试仓储 / In-memory test repository with OCC, leases, and fencing."""

    def __init__(self) -> None:
        """@brief 初始化仓储 / Initialize repository.

        @return None / None.
        """

        self.tasks: dict[VerificationKey, VerificationTask] = {}
        """@brief 聚合表 / Aggregate table."""
        self.next_attempt: dict[VerificationKey, datetime | None] = {}
        """@brief 下次领取时间 / Next claim times."""
        self.claims: dict[VerificationKey, tuple[str, datetime, int]] = {}
        """@brief 当前 fencing claims / Current fencing claims."""
        self.groups: set[ChatId] = set()
        """@brief 启用群组 / Enabled groups."""
        self._lock = asyncio.Lock()
        """@brief 原子操作锁 / Atomic-operation lock."""

    async def group_enabled(self, chat_id: ChatId) -> bool:
        """@brief 查询群组开关 / Read group switch.

        @param chat_id 群组 ID / Chat ID.
        @return 是否启用 / Whether enabled.
        """

        return chat_id in self.groups

    async def enable_group(self, chat_id: ChatId, group_name: str) -> None:
        """@brief 开启群组 / Enable group.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名 / Group name.
        @return None / None.
        """

        del group_name
        self.groups.add(chat_id)

    async def disable_group(self, chat_id: ChatId) -> None:
        """@brief 关闭群组 / Disable group.

        @param chat_id 群组 ID / Chat ID.
        @return None / None.
        """

        self.groups.discard(chat_id)

    async def create(
        self,
        task: VerificationTask,
        *,
        recover_at: datetime,
    ) -> VerificationTask:
        """@brief 创建或替换聚合 / Create or replace aggregate.

        @param task 创建聚合 / Creation aggregate.
        @param recover_at 恢复时间 / Recovery time.
        @return 规范聚合 / Canonical aggregate.
        """

        async with self._lock:
            existing = self.tasks.get(task.key)
            version = (
                VerificationVersion(0) if existing is None else existing.version.next()
            )
            canonical = replace(task, version=version)
            self.tasks[task.key] = canonical
            self.next_attempt[task.key] = recover_at
            self.claims.pop(task.key, None)
            return canonical

    async def load(self, key: VerificationKey) -> VerificationTask | None:
        """@brief 读取聚合 / Load aggregate.

        @param key 聚合键 / Aggregate key.
        @return 聚合或 None / Aggregate or None.
        """

        return self.tasks.get(key)

    async def apply(
        self,
        key: VerificationKey,
        *,
        expected_version: VerificationVersion,
        event: VerificationEvent,
        now: datetime,
        message_id: MessageId | None = None,
    ) -> VerificationTask:
        """@brief OCC 应用事件 / Apply event with OCC.

        @param key 聚合键 / Aggregate key.
        @param expected_version 预期版本 / Expected version.
        @param event 事件 / Event.
        @param now 时刻 / Time.
        @param message_id 可选消息 / Optional message.
        @return 更新聚合 / Updated aggregate.
        """

        async with self._lock:
            current = self.tasks[key]
            updated = current.evolve(
                event,
                expected_version=expected_version,
                now=now,
                message_id=message_id,
            )
            self.tasks[key] = updated
            self.next_attempt[key] = _next_attempt(updated, now)
            self.claims.pop(key, None)
            return updated

    async def claim_ready(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 有界领取就绪聚合 / Claim bounded ready aggregates.

        @param now 当前时刻 / Current time.
        @param limit 上限 / Limit.
        @param lease_for 租约 / Lease.
        @return claims / Claims.
        """

        return await self._claim(now=now, limit=limit, lease_for=lease_for, only=None)

    async def claim_one(
        self,
        key: VerificationKey,
        *,
        now: datetime,
        lease_for: timedelta,
    ) -> VerificationClaim | None:
        """@brief 领取单聚合 / Claim one aggregate.

        @param key 聚合键 / Aggregate key.
        @param now 当前时刻 / Current time.
        @param lease_for 租约 / Lease.
        @return claim 或 None / Claim or None.
        """

        claims = await self._claim(now=now, limit=1, lease_for=lease_for, only=key)
        return claims[0] if claims else None

    async def complete(
        self, claim: VerificationClaim, *, now: datetime
    ) -> VerificationTask:
        """@brief fencing 完成 claim / Complete claim with fencing.

        @param claim claim / Claim.
        @param now 时刻 / Time.
        @return 终态聚合 / Terminal aggregate.
        """

        async with self._lock:
            active = self.claims.get(claim.task.key)
            if active is None or active[0] != claim.token:
                raise VerificationFencingError("stale claim")
            current = self.tasks[claim.task.key]
            updated = current.evolve(
                VerificationEvent.EFFECT_DELIVERED,
                expected_version=claim.task.version,
                now=now,
            )
            self.tasks[claim.task.key] = updated
            self.next_attempt[claim.task.key] = None
            self.claims.pop(claim.task.key)
            return updated

    async def retry(
        self,
        claim: VerificationClaim,
        *,
        retry_at: datetime,
        error: str,
        now: datetime,
    ) -> None:
        """@brief fencing 安排重试 / Schedule retry with fencing.

        @param claim claim / Claim.
        @param retry_at 下次时刻 / Next time.
        @param error 错误 / Error.
        @param now 当前时刻 / Current time.
        @return None / None.
        """

        del error, now
        async with self._lock:
            active = self.claims.get(claim.task.key)
            if active is None or active[0] != claim.token:
                raise VerificationFencingError("stale claim")
            self.claims.pop(claim.task.key)
            self.next_attempt[claim.task.key] = retry_at

    async def recover_expired_leases(self, *, now: datetime) -> int:
        """@brief 回收过期 claims / Recover expired claims.

        @param now 当前时刻 / Current time.
        @return 回收数 / Recovery count.
        """

        async with self._lock:
            expired = [
                key
                for key, (_token, deadline, _attempt) in self.claims.items()
                if deadline <= now
            ]
            for key in expired:
                self.claims.pop(key)
                self.next_attempt[key] = now
            return len(expired)

    async def _claim(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
        only: VerificationKey | None,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 内存原子 claim / In-memory atomic claim.

        @param now 当前时刻 / Current time.
        @param limit 上限 / Limit.
        @param lease_for 租约 / Lease.
        @param only 可选键 / Optional key.
        @return claims / Claims.
        """

        async with self._lock:
            candidates = [
                key
                for key, due in self.next_attempt.items()
                if due is not None
                and due <= now
                and key not in self.claims
                and (only is None or key == only)
                and not self.tasks[key].status.terminal
            ][:limit]
            results: list[VerificationClaim] = []
            for key in candidates:
                task = self.tasks[key]
                if task.status is VerificationStatus.CREATING:
                    task = task.evolve(
                        VerificationEvent.ABORT_CREATION,
                        expected_version=task.version,
                        now=now,
                    )
                elif task.status is VerificationStatus.PENDING:
                    task = task.evolve(
                        VerificationEvent.DEADLINE_REACHED,
                        expected_version=task.version,
                        now=now,
                    )
                self.tasks[key] = task
                previous_attempt = self.claims.get(key, ("", now, 0))[2]
                token = str(uuid.uuid4())
                attempt = previous_attempt + 1
                deadline = now + lease_for
                self.claims[key] = (token, deadline, attempt)
                results.append(VerificationClaim(task, token, deadline, attempt))
            return tuple(results)


class RecordingDelivery:
    """@brief 可阻塞的副作用记录端口 / Blockable effect-recording port."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recording.

        @return None / None.
        """

        self.calls: list[VerificationTask] = []
        """@brief 投递调用 / Delivery calls."""
        self.started = asyncio.Event()
        """@brief 投递开始通知 / Delivery-start notification."""
        self.release: asyncio.Event | None = None
        """@brief 可选阻塞门 / Optional blocking gate."""

    async def deliver(self, task: VerificationTask) -> None:
        """@brief 记录并可选阻塞 / Record and optionally block.

        @param task 过渡态聚合 / Transitional aggregate.
        @return None / None.
        """

        self.calls.append(task)
        self.started.set()
        if self.release is not None:
            await self.release.wait()


class OneShotClaimFailureRepository(MemoryVerificationRepository):
    """@brief 首次 claim 失败后恢复的测试仓储 / Test repository recovering after its first claim failure."""

    def __init__(self) -> None:
        """@brief 初始化单次故障 / Initialize the one-shot failure.

        @return None / None.
        """

        super().__init__()
        self.failures_remaining = 1
        """@brief 剩余注入故障数 / Remaining injected failures."""

    async def claim_ready(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 首次抛出瞬态错误，随后正常 claim / Raise once, then claim normally.

        @param now 当前时刻 / Current time.
        @param limit 上限 / Limit.
        @param lease_for 租约 / Lease.
        @return 首次之后的 claims / Claims after the first call.
        @raises RuntimeError 首次模拟数据库瞬态错误 / Simulated transient database error on first call.
        """

        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary database failure")
        return await super().claim_ready(now=now, limit=limit, lease_for=lease_for)


class OverclaimingVerificationRepository(MemoryVerificationRepository):
    """@brief 模拟违反 claim limit 的验证仓储 / Verification repository violating the claim limit."""

    def __init__(self) -> None:
        """@brief 初始化超额观测事件 / Initialize the overclaim observation event."""

        super().__init__()
        self.overclaimed = asyncio.Event()

    async def claim_ready(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 忽略配置上限多领取一项 / Claim one item beyond the configured limit."""

        claims = await self._claim(
            now=now,
            limit=limit + 1,
            lease_for=lease_for,
            only=None,
        )
        if len(claims) > limit:
            self.overclaimed.set()
        return claims


class FailingRecoveryRepository(MemoryVerificationRepository):
    """@brief 启动 lease 恢复失败的测试仓储 / Test repository whose startup lease recovery fails."""

    async def recover_expired_leases(self, *, now: datetime) -> int:
        """@brief 抛出固定恢复故障 / Raise a fixed recovery failure.

        @param now 当前时刻 / Current time.
        @return 不返回 / Does not return.
        @raises RuntimeError 固定测试故障 / Fixed test failure.
        """

        del now
        raise RuntimeError("lease recovery failed")


def _next_attempt(task: VerificationTask, now: datetime) -> datetime | None:
    """@brief 推导测试调度时间 / Derive test schedule time.

    @param task 聚合 / Aggregate.
    @param now 当前时刻 / Current time.
    @return 下次时间 / Next time.
    """

    if task.status is VerificationStatus.PENDING:
        return task.expires_at
    if task.status.needs_delivery:
        return now
    return None


async def _active(
    service: VerificationService,
    *,
    key: VerificationKey = KEY,
) -> tuple[str, VerificationTask]:
    """@brief 创建并激活测试验证 / Create and activate a test verification.

    @param service 验证服务 / Verification service.
    @param key 聚合键 / Aggregate key.
    @return token 与 PENDING 聚合 / Token and PENDING aggregate.
    """

    invitation = await service.begin(key, member_name="Alice")
    pending = await service.activate(invitation, MessageId(7))
    return invitation.token, pending


async def _run_worker(
    worker: VerificationTimeoutWorker,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """@brief 通过唯一 BackgroundService 契约运行 worker / Run a worker through the sole BackgroundService contract.

    @param worker 验证超时 worker / Verification timeout worker.
    @return 停止信号与运行任务 / Stop signal and run task.
    """

    stop_event = asyncio.Event()
    run_task = asyncio.create_task(worker.run(stop_event))
    while worker.state is VerificationWorkerState.NEW:
        if run_task.done():
            await run_task
        await asyncio.sleep(0)
    assert worker.state is VerificationWorkerState.RUNNING
    return stop_event, run_task


async def _stop_worker(
    stop_event: asyncio.Event,
    run_task: asyncio.Task[None],
) -> None:
    """@brief 请求正常停止并等待已领取批次 / Request normal stop and await claimed batches.

    @param stop_event 停止信号 / Stop signal.
    @param run_task worker 运行任务 / Worker run task.
    @return None / None.
    """

    stop_event.set()
    await run_task


def test_worker_stops_immediately_when_the_runtime_signal_is_already_set() -> None:
    """@brief 已置位停止信号在启动恢复后立即关闭 / An already-set stop signal closes immediately after startup recovery."""

    async def scenario() -> None:
        """@brief 以早停信号运行 worker / Run the worker with an early stop signal.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        service = VerificationService(
            repository=repository, delivery=RecordingDelivery()
        )
        worker = VerificationTimeoutWorker(repository=repository, service=service)
        stop_event = asyncio.Event()
        stop_event.set()

        await worker.run(stop_event)

        assert worker.state is VerificationWorkerState.CLOSED

    asyncio.run(scenario())


def test_worker_run_rejects_concurrent_and_post_close_reuse() -> None:
    """@brief worker 实例不能重复运行 / A worker instance cannot be run more than once."""

    async def scenario() -> None:
        """@brief 验证一次性生命周期 / Verify the one-shot lifecycle.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        service = VerificationService(
            repository=repository, delivery=RecordingDelivery()
        )
        worker = VerificationTimeoutWorker(repository=repository, service=service)
        stop_event, run_task = await _run_worker(worker)

        with pytest.raises(RuntimeError, match="cannot run from running"):
            await worker.run(asyncio.Event())
        await _stop_worker(stop_event, run_task)
        with pytest.raises(RuntimeError, match="cannot run from closed"):
            await worker.run(asyncio.Event())

    asyncio.run(scenario())


def test_worker_rejects_a_repository_batch_larger_than_its_claim_limit() -> None:
    """@brief 超额 verification batch 不得绕过 worker 容量 / An oversized verification batch cannot bypass worker capacity."""

    async def scenario() -> None:
        """@brief 创建两个就绪任务并返回超额 batch / Create two due tasks and return an oversized batch."""

        clock = ManualClock()
        repository = OverclaimingVerificationRepository()
        delivery = RecordingDelivery()
        service = VerificationService(
            repository=repository,
            delivery=delivery,
            clock=clock,
        )
        await service.begin(KEY, member_name="Alice")
        await service.begin(
            VerificationKey(ChatId(-1001), UserId(43)),
            member_name="Bob",
        )
        clock.advance(timedelta(seconds=31))
        worker = VerificationTimeoutWorker(
            repository=repository,
            service=service,
            worker_count=1,
            claim_limit=1,
            poll_interval=0.01,
            clock=clock,
        )
        stop_event, run_task = await _run_worker(worker)

        await asyncio.wait_for(repository.overclaimed.wait(), timeout=1)
        await _stop_worker(stop_event, run_task)

        assert delivery.calls == []

    asyncio.run(scenario())


def test_worker_startup_failure_propagates_and_closes_the_instance() -> None:
    """@brief 启动恢复故障原样传播并终结实例 / A startup-recovery failure propagates and closes the instance."""

    async def scenario() -> None:
        """@brief 注入 lease 恢复故障 / Inject a lease-recovery failure.

        @return None / None.
        """

        repository = FailingRecoveryRepository()
        service = VerificationService(
            repository=repository, delivery=RecordingDelivery()
        )
        worker = VerificationTimeoutWorker(repository=repository, service=service)

        with pytest.raises(RuntimeError, match="lease recovery failed"):
            await worker.run(asyncio.Event())

        assert worker.state is VerificationWorkerState.CLOSED

    asyncio.run(scenario())


def test_stale_callback_is_rejected_before_any_external_effect() -> None:
    """@brief 陈旧 callback 在副作用前拒绝 / A stale callback is rejected before external effects."""

    async def scenario() -> None:
        """@brief 驱动陈旧 callback / Drive stale callback.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        delivery = RecordingDelivery()
        service = VerificationService(
            repository=repository, delivery=delivery, clock=ManualClock()
        )
        token, pending = await _active(service)

        result = await service.request_pass(
            KEY,
            expected_version=VerificationVersion(pending.version.value - 1),
            token=token,
        )

        assert isinstance(result, VerificationRejected)
        assert result.code is VerificationRejectionCode.STALE_VERSION
        assert delivery.calls == []

    asyncio.run(scenario())


def test_timeout_and_pass_compare_and_swap_one_version() -> None:
    """@brief timeout 与 pass 对同一版本最多一个提交 / Timeout and pass can commit at most once for one version."""

    async def scenario() -> None:
        """@brief 并发应用互斥事件 / Concurrently apply mutually exclusive events.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        clock = ManualClock()
        service = VerificationService(
            repository=repository, delivery=RecordingDelivery(), clock=clock
        )
        _token, pending = await _active(service)

        results = await asyncio.gather(
            repository.apply(
                KEY,
                expected_version=pending.version,
                event=VerificationEvent.PASS_REQUESTED,
                now=NOW + timedelta(minutes=1),
            ),
            repository.apply(
                KEY,
                expected_version=pending.version,
                event=VerificationEvent.DEADLINE_REACHED,
                now=pending.expires_at,
            ),
            return_exceptions=True,
        )

        assert sum(isinstance(result, VerificationTask) for result in results) == 1
        assert (
            sum(isinstance(result, StaleVerificationVersion) for result in results) == 1
        )

    asyncio.run(scenario())


def test_abandoned_creation_is_recovered_and_compensated() -> None:
    """@brief CREATING commit→Telegram crash gap 由 durable worker 补偿 / Durable worker compensates the CREATING commit-to-Telegram crash gap."""

    async def scenario() -> None:
        """@brief 不激活创建流程并推进恢复时间 / Leave creation unactivated and advance recovery time.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        delivery = RecordingDelivery()
        clock = ManualClock()
        service = VerificationService(
            repository=repository, delivery=delivery, clock=clock
        )
        await service.begin(KEY, member_name="Alice")
        clock.advance(timedelta(seconds=31))
        worker = VerificationTimeoutWorker(
            repository=repository,
            service=service,
            worker_count=1,
            poll_interval=0.01,
            clock=clock,
        )
        stop_event, run_task = await _run_worker(worker)
        try:
            async with asyncio.timeout(1):
                while repository.tasks[KEY].status is not VerificationStatus.CANCELLED:
                    await asyncio.sleep(0)
        finally:
            await _stop_worker(stop_event, run_task)

        assert [task.status for task in delivery.calls] == [
            VerificationStatus.CANCELLING
        ]
        assert repository.tasks[KEY].status is VerificationStatus.CANCELLED

    asyncio.run(scenario())


def test_lease_recovery_replays_ambiguous_effect_and_fences_old_claim() -> None:
    """@brief send→ack crash 后租约恢复以 at-least-once 重放并 fencing 旧 claim / Lease recovery replays after send-to-ack crash and fences the old claim."""

    async def scenario() -> None:
        """@brief 模拟 Telegram 成功但未 ack 的崩溃 / Simulate Telegram success followed by pre-ack crash.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        delivery = RecordingDelivery()
        clock = ManualClock()
        service = VerificationService(
            repository=repository, delivery=delivery, clock=clock
        )
        _token, pending = await _active(service)
        await repository.apply(
            KEY,
            expected_version=pending.version,
            event=VerificationEvent.PASS_REQUESTED,
            now=clock.now(),
        )
        first = await repository.claim_one(
            KEY, now=clock.now(), lease_for=timedelta(seconds=5)
        )
        assert first is not None
        await delivery.deliver(first.task)

        clock.advance(timedelta(seconds=6))
        assert await service.process_claim(first) is False
        assert len(delivery.calls) == 1
        assert await repository.recover_expired_leases(now=clock.now()) == 1
        second = await repository.claim_one(
            KEY, now=clock.now(), lease_for=timedelta(seconds=5)
        )
        assert second is not None
        assert second.token != first.token
        assert await service.process_claim(second) is True
        try:
            await repository.complete(first, now=clock.now())
        except VerificationFencingError:
            pass
        else:
            raise AssertionError("old claim was not fenced")

        assert len(delivery.calls) == 2
        assert repository.tasks[KEY].status is VerificationStatus.PASSED

    asyncio.run(scenario())


def test_shutdown_drains_inflight_delivery_without_detached_tasks() -> None:
    """@brief shutdown 等待当前批次且不遗留 detached task / Shutdown drains the current batch without detached tasks."""

    async def scenario() -> None:
        """@brief 阻塞投递并启动 shutdown / Block delivery and start shutdown.

        @return None / None.
        """

        repository = MemoryVerificationRepository()
        delivery = RecordingDelivery()
        delivery.release = asyncio.Event()
        clock = ManualClock()
        service = VerificationService(
            repository=repository, delivery=delivery, clock=clock
        )
        _token, pending = await _active(service)
        await repository.apply(
            KEY,
            expected_version=pending.version,
            event=VerificationEvent.PASS_REQUESTED,
            now=clock.now(),
        )
        worker = VerificationTimeoutWorker(
            repository=repository,
            service=service,
            worker_count=1,
            poll_interval=0.01,
            clock=clock,
        )
        stop_event, run_task = await _run_worker(worker)
        await delivery.started.wait()
        stop_event.set()
        await asyncio.sleep(0)
        assert not run_task.done()
        delivery.release.set()
        await run_task

        assert worker.state is VerificationWorkerState.CLOSED
        assert repository.tasks[KEY].status is VerificationStatus.PASSED

    asyncio.run(scenario())


def test_worker_survives_a_transient_claim_failure_without_losing_capacity() -> None:
    """@brief 单次数据库错误不会静默杀死固定 worker / One database error does not silently kill a fixed worker."""

    async def scenario() -> None:
        """@brief 注入一次 claim 错误后等待同一 worker 恢复 / Inject one claim error and await recovery by the same worker.

        @return None / None.
        """

        repository = OneShotClaimFailureRepository()
        delivery = RecordingDelivery()
        clock = ManualClock()
        service = VerificationService(
            repository=repository, delivery=delivery, clock=clock
        )
        _token, pending = await _active(service)
        await repository.apply(
            KEY,
            expected_version=pending.version,
            event=VerificationEvent.PASS_REQUESTED,
            now=clock.now(),
        )
        worker = VerificationTimeoutWorker(
            repository=repository,
            service=service,
            worker_count=1,
            poll_interval=0.01,
            clock=clock,
        )

        stop_event, run_task = await _run_worker(worker)
        try:
            async with asyncio.timeout(1):
                while repository.tasks[KEY].status is not VerificationStatus.PASSED:
                    await asyncio.sleep(0)
        finally:
            await _stop_worker(stop_event, run_task)

        assert repository.failures_remaining == 0
        assert len(delivery.calls) == 1
        assert worker.state is VerificationWorkerState.CLOSED

    asyncio.run(scenario())
