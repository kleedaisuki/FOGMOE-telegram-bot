"""@brief Durable Retrieval worker 测试 / Tests for the durable Retrieval worker."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.application.retrieval import (
    EpisodicPassageRenderer,
    EpisodicTurn,
    PassageVectorClaim,
    RetrievalWorker,
    RetryableEmbeddingError,
)
from fogmoe_bot.application.runtime import AdaptivePollingPolicy, UtcClock
from fogmoe_bot.domain.observability.signals import MetricSignal, SpanSignal
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
)

NOW = datetime(2034, 1, 1, tzinfo=UTC)
"""@brief 确定性 worker 时间 / Deterministic worker instant."""


class _Clock(UtcClock):
    """@brief 固定 UTC clock / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed instant."""

        return NOW


class _RecoveryClock(UtcClock):
    """@brief 同步推进 UTC 与单调时间的测试时钟 / Test clock advancing UTC and monotonic time together."""

    def __init__(self) -> None:
        """@brief 从固定时刻与零单调秒开始 / Start at the fixed instant and zero monotonic seconds."""

        self.current = NOW
        self.monotonic_seconds = 0.0

    def now(self) -> datetime:
        """@brief 返回当前 UTC 时刻 / Return the current UTC instant.

        @return 当前测试时刻 / Current test instant.
        """

        return self.current

    def monotonic(self) -> float:
        """@brief 返回当前单调秒数 / Return current monotonic seconds.

        @return 当前单调秒数 / Current monotonic seconds.
        """

        return self.monotonic_seconds

    def advance(self, duration: timedelta) -> None:
        """@brief 同步推进两个时间轴 / Advance both time axes together.

        @param duration 正时间增量 / Positive duration.
        @return None / None.
        """

        if duration <= timedelta():
            raise ValueError("Test-clock advance must be positive")
        self.current += duration
        self.monotonic_seconds += duration.total_seconds()


class _Source:
    """@brief 只返回一次 Turn 的 source / Source returning one turn once."""

    def __init__(self, turn: EpisodicTurn) -> None:
        """@brief 保存 Turn / Store the turn."""

        self._turn = turn
        self._returned = False
        self.reader_tasks: list[str] = []

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 第一次返回来源，之后为空 / Return the source once and then remain empty."""

        assert format_version == 1
        assert limit == 4
        task = asyncio.current_task()
        self.reader_tasks.append(task.get_name() if task is not None else "")
        if self._returned:
            return ()
        self._returned = True
        return (self._turn,)


class _Store:
    """@brief 记录 projection 与 vector transition 的 store fake / Store fake recording projection and vector transitions."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 初始化状态 / Initialize state."""

        self._stop_event = stop_event
        self.space: EmbeddingSpace | None = None
        self.passage: RetrievalPassage | None = None
        self.claimed = False
        self.completed: EmbeddingVector | None = None
        self.retried_at: datetime | None = None

    async def ensure_space(self, space: EmbeddingSpace) -> None:
        """@brief 记录空间 / Record the space."""

        self.space = space

    async def project_turn(
        self,
        turn: EpisodicTurn,
        passages: Sequence[RetrievalPassage],
        *,
        space: EmbeddingSpace,
        projected_at: datetime,
    ) -> None:
        """@brief 记录唯一 passage / Record the sole passage."""

        assert turn.turn_id == passages[0].source_id
        assert projected_at == NOW
        assert space == self.space
        self.passage = passages[0]

    async def claim_vectors(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[PassageVectorClaim, ...]:
        """@brief passage 出现后领取一次 / Claim once after the passage appears."""

        assert now == NOW
        assert limit == 4
        assert lease_for == timedelta(seconds=30)
        if self.passage is None or self.claimed:
            return ()
        self.claimed = True
        return (
            PassageVectorClaim(
                passage=self.passage,
                space=space,
                claim_token=UUID("00000000-0000-0000-0000-000000000099"),
                attempt_count=1,
            ),
        )

    async def complete_vector(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
        *,
        completed_at: datetime,
    ) -> None:
        """@brief 记录完成并停止 worker / Record completion and stop the worker."""

        assert claim.passage == self.passage
        assert completed_at == NOW
        self.completed = vector
        self._stop_event.set()

    async def retry_vector(
        self,
        claim: PassageVectorClaim,
        *,
        retry_at: datetime,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief 记录 retry 并停止 worker / Record a retry and stop the worker."""

        assert claim.passage == self.passage
        assert "RetryableEmbeddingError" in error
        assert failed_at == NOW
        self.retried_at = retry_at
        self._stop_event.set()

    async def fail_vector(
        self,
        claim: PassageVectorClaim,
        *,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief 本场景不允许 final failure / Reject final failure in this scenario."""

        raise AssertionError((claim, error, failed_at))

    async def recover_expired_vector_leases(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
    ) -> int:
        """@brief 验证启动恢复调用 / Verify startup recovery."""

        assert space == self.space
        assert now == NOW
        return 0

    async def search(
        self,
        *,
        scope: RetrievalScope,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief Worker 测试不执行 search / Worker tests do not execute search."""

        raise AssertionError((scope, corpus_id, space, query_vector, limit))


class _FailOnceEnsureSpaceStore(_Store):
    """@brief 首次初始化空间失败的 store 替身 / Store double whose first space initialization fails."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 初始化父 store 与调用计数 / Initialize the parent store and call count."""

        super().__init__(stop_event)
        self.ensure_calls = 0

    async def ensure_space(self, space: EmbeddingSpace) -> None:
        """@brief 注入一次临时初始化错误 / Inject one transient initialization error.

        @param space embedding 空间 / Embedding space.
        @return None / None.
        """

        self.ensure_calls += 1
        if self.ensure_calls == 1:
            raise ValueError("Span duration cannot be negative")
        await super().ensure_space(space)


class _Embeddings:
    """@brief 可切换成功/重试的 embedding fake / Embedding fake switching between success and retry."""

    def __init__(self, *, fail: bool) -> None:
        """@brief 保存失败开关 / Store the failure switch."""

        self._fail = fail

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        space: EmbeddingSpace,
    ) -> tuple[EmbeddingVector, ...]:
        """@brief 返回向量或 provider retry / Return a vector or provider retry."""

        assert texts and "User:" in texts[0]
        if self._fail:
            raise RetryableEmbeddingError(
                "rate limited",
                retry_after=timedelta(seconds=7),
            )
        return (EmbeddingVector((1.0, 0.0)),)

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief Worker 测试不执行 Query / Worker tests do not embed queries."""

        raise AssertionError((text, space))


class _FailOncePollSource:
    """@brief 首次轮询失败、第二次停止的 source 替身 / Source double that fails once then stops on the second poll."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        """@brief 保存停止信号与调用次数 / Store the stop signal and invocation count."""

        self._stop_event = stop_event
        self.calls = 0

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 注入一次临时持久化错误 / Inject one transient persistence failure.

        @param format_version passage 格式版本 / Passage format version.
        @param limit 读取上限 / Read limit.
        @return 空来源 / Empty source batch.
        """

        assert format_version == 1 and limit == 4
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary database polling failure")
        self._stop_event.set()
        return ()


class _OneTurnThenStopSource:
    """@brief 先返回一条来源、随后停止的 source 替身 / Source double returning one item then stopping."""

    def __init__(self, turn: EpisodicTurn, stop_event: asyncio.Event) -> None:
        """@brief 保存一次性来源与停止信号 / Store the one-shot source and stop signal."""

        self._turn = turn
        self._stop_event = stop_event
        self.calls = 0

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 首轮返回来源，下一轮请求停止 / Return a source first, then request stop.

        @param format_version passage 格式版本 / Passage format version.
        @param limit 读取上限 / Read limit.
        @return 第一轮的一条来源，之后为空 / One source on the first pass, then empty.
        """

        assert format_version == 1 and limit == 4
        self.calls += 1
        if self.calls == 1:
            return (self._turn,)
        self._stop_event.set()
        return ()


class _BlockingSource:
    """@brief 阻塞到取消的 source 替身 / Source double that blocks until cancellation."""

    def __init__(self) -> None:
        """@brief 初始化开始同步点 / Initialize the start synchronization point."""

        self.started = asyncio.Event()

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 等待外部取消 / Wait for external cancellation.

        @param format_version passage 格式版本 / Passage format version.
        @param limit 读取上限 / Read limit.
        @return 永不返回 / Never returns.
        """

        assert format_version == 1 and limit == 4
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking source unexpectedly resumed")


class _RecoveryCadenceSource:
    """@brief 在恢复 deadline 前后推进测试时钟 / Advance the test clock around a recovery deadline."""

    def __init__(
        self,
        *,
        clock: _RecoveryClock,
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 保存时钟与停止信号 / Store the clock and stop signal.

        @param clock 可推进测试时钟 / Advanceable test clock.
        @param stop_event worker 停止信号 / Worker stop signal.
        """

        self._clock = clock
        self._stop_event = stop_event
        self.calls = 0

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> tuple[EpisodicTurn, ...]:
        """@brief 先停在 deadline 前，再跨过 deadline / Stop just before and then cross the deadline.

        @param format_version passage 格式版本 / Passage format version.
        @param limit 读取上限 / Read limit.
        @return 始终为空的来源批次 / Always-empty source batch.
        """

        assert format_version == 1 and limit == 4
        self.calls += 1
        if self.calls == 1:
            self._clock.advance(timedelta(seconds=4, milliseconds=999))
        elif self.calls == 2:
            self._clock.advance(timedelta(milliseconds=1))
        else:
            self._stop_event.set()
        return ()


class _RecoveryCadenceStore(_Store):
    """@brief 记录周期恢复并模拟未过期活动 lease / Record periodic recovery with an unexpired active lease."""

    def __init__(
        self,
        stop_event: asyncio.Event,
        *,
        fail_first: bool,
    ) -> None:
        """@brief 初始化记录与首次故障开关 / Initialize records and first-failure switch.

        @param stop_event worker 停止信号 / Worker stop signal.
        @param fail_first 首次恢复是否失败 / Whether the first recovery fails.
        """

        super().__init__(stop_event)
        self.recovery_times: list[datetime] = []
        self.claim_polls = 0
        self._fail_first = fail_first
        self.active_lease_expires_at = NOW + timedelta(seconds=30)
        self.stole_active_lease = False

    async def claim_vectors(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[PassageVectorClaim, ...]:
        """@brief 保持 vector consumer 空闲但可运行 / Keep vector consumers idle but operational.

        @param space 当前 embedding 空间 / Active embedding space.
        @param now 当前 UTC 时刻 / Current UTC instant.
        @param limit 领取上限 / Claim limit.
        @param lease_for claim lease / Claim lease.
        @return 空 claim 批次 / Empty claim batch.
        """

        assert space == self.space and limit == 4
        assert lease_for == timedelta(seconds=30)
        self.claim_polls += 1
        return ()

    async def recover_expired_vector_leases(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
    ) -> int:
        """@brief 记录恢复，并拒绝把未过期 lease 当作可恢复 / Record recovery without treating an unexpired lease as recoverable.

        @param space 当前 embedding 空间 / Active embedding space.
        @param now 恢复判定时刻 / Recovery decision instant.
        @return 本测试始终没有过期 row / No expired row in this test.
        """

        assert space == self.space
        self.recovery_times.append(now)
        if self._fail_first and len(self.recovery_times) == 1:
            raise RuntimeError("temporary vector lease-recovery failure")
        if now >= self.active_lease_expires_at:
            self.stole_active_lease = True
            return 1
        return 0


class _FailOnceTelemetry(Telemetry):
    """@brief 首次 gauge 抛出 telemetry 错误的 recorder / Telemetry recorder whose first gauge raises an error."""

    def __init__(self) -> None:
        """@brief 初始化基础缓冲与失败开关 / Initialize the base buffer and failure switch."""

        super().__init__(TelemetryBuffer(64))
        self._fail_next_gauge = True

    def gauge(
        self,
        name: str,
        value: float,
        *,
        unit: str = "1",
        attributes: Mapping[str, object] | None = None,
    ) -> bool:
        """@brief 模拟一次 telemetry 时间戳校验失败 / Simulate one telemetry timestamp-validation failure.

        @param name metric 名称 / Metric name.
        @param value metric 值 / Metric value.
        @param unit metric 单位 / Metric unit.
        @param attributes metric 属性 / Metric attributes.
        @return 缓冲接收结果 / Buffer acceptance result.
        """

        if self._fail_next_gauge:
            self._fail_next_gauge = False
            raise ValueError("Span duration cannot be negative")
        return super().gauge(name, value, unit=unit, attributes=attributes)


def _worker(
    *, fail: bool, stop_event: asyncio.Event
) -> tuple[RetrievalWorker, _Store, _Source, TelemetryBuffer]:
    """@brief 构造固定 worker 场景 / Build a fixed worker scenario."""

    turn = EpisodicTurn(
        turn_id=UUID("00000000-0000-0000-0000-000000000042"),
        scope=RetrievalScope("personal", 42),
        user_text="I prefer tea",
        assistant_text="Noted",
        occurred_at=NOW,
    )
    space = EmbeddingSpace(
        "test.worker.v1",
        "test/model",
        2,
        "Retrieve relevant evidence.",
        1,
    )
    store = _Store(stop_event)
    source = _Source(turn)
    telemetry_buffer = TelemetryBuffer(64)
    worker = RetrievalWorker(
        source=source,
        store=store,
        embeddings=_Embeddings(fail=fail),
        space=space,
        renderer=EpisodicPassageRenderer(),
        telemetry=Telemetry(telemetry_buffer),
        worker_count=4,
        batch_size=4,
        polling_policy=AdaptivePollingPolicy(0.01, 0.02, jitter_ratio=0.0),
        lease_for=timedelta(seconds=30),
        clock=_Clock(),
    )
    return worker, store, source, telemetry_buffer


def _resilient_worker(
    *,
    source: object,
    store: _Store,
    telemetry: Telemetry,
) -> RetrievalWorker:
    """@brief 构造只验证轮询韧性的 worker / Build a worker used only to validate polling resilience.

    @param source 测试 source 端口 / Test source port.
    @param store 测试检索存储 / Test retrieval store.
    @param telemetry 测试 telemetry recorder / Test telemetry recorder.
    @return 配置好的 worker / Configured worker.
    """

    space = EmbeddingSpace(
        "test.worker.v1",
        "test/model",
        2,
        "Retrieve relevant evidence.",
        1,
    )
    return RetrievalWorker(
        source=source,  # type: ignore[arg-type]
        store=store,
        embeddings=_Embeddings(fail=False),
        space=space,
        renderer=EpisodicPassageRenderer(),
        telemetry=telemetry,
        worker_count=1,
        batch_size=4,
        polling_policy=AdaptivePollingPolicy(0.001, 0.004, jitter_ratio=0.0),
        lease_for=timedelta(seconds=30),
        clock=_Clock(),
    )


def test_worker_projects_embeds_and_drains_structurally() -> None:
    """@brief Worker 在一个 owned TaskGroup 中完成 projection 与 embedding / Worker completes projection and embedding in one owned TaskGroup."""

    async def scenario() -> None:
        """@brief 执行成功场景 / Execute the success scenario."""

        stop_event = asyncio.Event()
        worker, store, source, telemetry_buffer = _worker(
            fail=False, stop_event=stop_event
        )
        await worker.run(stop_event)
        assert store.completed == EmbeddingVector((1.0, 0.0))
        assert store.retried_at is None
        assert source.reader_tasks
        assert set(source.reader_tasks) == {"retrieval-projection"}
        signals = telemetry_buffer.drain(64)
        assert [
            signal.name for signal in signals if isinstance(signal, SpanSignal)
        ] == ["retrieval.projection.batch", "retrieval.embedding.batch"]
        assert {
            signal.name for signal in signals if isinstance(signal, MetricSignal)
        } >= {
            "fogmoe.retrieval.outcomes",
            "fogmoe.retrieval.batch.size",
            "fogmoe.retrieval.source.discovery.duration",
            "fogmoe.retrieval.vector.claim.duration",
        }

    asyncio.run(scenario())


def test_worker_honors_provider_retry_after() -> None:
    """@brief Retry-After 直接成为 durable retry 下界 / Retry-After becomes the durable retry boundary."""

    async def scenario() -> None:
        """@brief 执行 retry 场景 / Execute the retry scenario."""

        stop_event = asyncio.Event()
        worker, store, source, _ = _worker(fail=True, stop_event=stop_event)
        await worker.run(stop_event)
        assert store.completed is None
        assert store.retried_at == NOW + timedelta(seconds=7)
        assert set(source.reader_tasks) == {"retrieval-projection"}

    asyncio.run(scenario())


def test_transient_source_poll_failure_does_not_escape_worker_task_group() -> None:
    """@brief 单次 source 轮询故障不会终止 retrieval TaskGroup / One source-poll failure does not terminate the retrieval TaskGroup."""

    async def scenario() -> None:
        """@brief 验证后续轮询仍可执行并正常停止 / Verify a later poll still runs and stops normally."""

        stop_event = asyncio.Event()
        source = _FailOncePollSource(stop_event)
        worker = _resilient_worker(
            source=source,
            store=_Store(stop_event),
            telemetry=Telemetry(TelemetryBuffer(64)),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert source.calls >= 2

    asyncio.run(scenario())


def test_transient_space_initialization_failure_retries_without_runtime_failure() -> (
    None
):
    """@brief embedding 空间初始化的短暂故障会等待重试 / A transient embedding-space initialization failure waits and retries."""

    async def scenario() -> None:
        """@brief 验证初始化成功后 worker 仍能进入正常轮询 / Verify normal polling begins after initialization succeeds."""

        stop_event = asyncio.Event()
        source = _FailOncePollSource(stop_event)
        store = _FailOnceEnsureSpaceStore(stop_event)
        worker = _resilient_worker(
            source=source,
            store=store,
            telemetry=Telemetry(TelemetryBuffer(64)),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert store.ensure_calls >= 2
        assert source.calls >= 2

    asyncio.run(scenario())


def test_telemetry_poll_failure_does_not_escape_worker_task_group() -> None:
    """@brief 单次 telemetry 失败不会终止 retrieval TaskGroup / One telemetry failure does not terminate the retrieval TaskGroup."""

    async def scenario() -> None:
        """@brief 在下一轮安全退出，证明前一轮错误已隔离 / Exit safely on the next pass, proving the prior fault was isolated."""

        stop_event = asyncio.Event()
        turn = EpisodicTurn(
            turn_id=UUID("00000000-0000-0000-0000-000000000043"),
            scope=RetrievalScope("personal", 42),
            user_text="I prefer tea",
            assistant_text="Noted",
            occurred_at=NOW,
        )
        source = _OneTurnThenStopSource(turn, stop_event)
        worker = _resilient_worker(
            source=source,
            store=_Store(stop_event),
            telemetry=_FailOnceTelemetry(),
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert source.calls >= 2

    asyncio.run(scenario())


def test_retrieval_poll_cancellation_still_propagates() -> None:
    """@brief retrieval 轮询取消不得被故障隔离吞掉 / Retrieval-poll cancellation must not be swallowed by fault isolation."""

    async def scenario() -> None:
        """@brief 取消阻塞的 source pass 并验证传播 / Cancel a blocked source pass and verify propagation."""

        stop_event = asyncio.Event()
        source = _BlockingSource()
        worker = _resilient_worker(
            source=source,
            store=_Store(stop_event),
            telemetry=Telemetry(TelemetryBuffer(64)),
        )
        task = asyncio.create_task(worker.run(stop_event))
        await asyncio.wait_for(source.started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_first", [False, True], ids=["steady", "retry"])
def test_runtime_lease_recovery_uses_cadence_without_early_steal(
    fail_first: bool,
) -> None:
    """@brief Retrieval 运行期按 cadence 恢复且异常不阻断 projection/claim / Retrieval recovers on cadence without early stealing, and faults do not block projection or claims.

    @param fail_first 首次恢复是否注入故障 / Whether to inject a first recovery failure.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 跨过五秒 cadence 并验证两个故障边界 / Cross the five-second cadence and verify both fault boundaries.

        @return None / None.
        """

        stop_event = asyncio.Event()
        clock = _RecoveryClock()
        source = _RecoveryCadenceSource(clock=clock, stop_event=stop_event)
        store = _RecoveryCadenceStore(stop_event, fail_first=fail_first)
        worker = RetrievalWorker(
            source=source,
            store=store,
            embeddings=_Embeddings(fail=False),
            space=EmbeddingSpace(
                "test.worker.v1",
                "test/model",
                2,
                "Retrieve relevant evidence.",
                1,
            ),
            renderer=EpisodicPassageRenderer(),
            telemetry=Telemetry(TelemetryBuffer(64)),
            worker_count=1,
            batch_size=4,
            polling_policy=AdaptivePollingPolicy(
                0.001,
                0.004,
                jitter_ratio=0.0,
            ),
            lease_for=timedelta(seconds=30),
            clock=clock,
            recovery_monotonic=clock.monotonic,
        )

        await asyncio.wait_for(worker.run(stop_event), timeout=1)

        assert store.recovery_times == [NOW, NOW + timedelta(seconds=5)]
        assert not store.stole_active_lease
        assert source.calls == 3
        assert store.claim_polls > 0

    asyncio.run(scenario())
