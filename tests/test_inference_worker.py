"""@brief 可恢复推理活动 worker 测试 / Tests for the recoverable inference-activity worker."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.application.conversation.inference_worker import (
    FullJitterInferenceRetryPolicy,
    InferenceErrorCategory,
    InferenceDependencyPending,
    InferenceOutboundIntent,
    InferenceResult,
    InferenceRuntimeLimits,
    InferenceWorker,
    PermanentInferenceError,
    RetryableInferenceError,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    InferenceActivityId,
    LeaseToken,
    TurnId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityDraft,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import MessageDraft
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)


NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _claim(*, attempt_count: int = 1) -> InferenceActivityClaim:
    """@brief 构造 processing 推理 claim / Build a processing inference claim.

    @param attempt_count 已领取次数 / Recorded claim count.
    @return 测试 claim / Test claim.
    """

    turn_id = TurnId.new()
    draft = InferenceActivityDraft(
        activity_id=InferenceActivityId.for_turn(turn_id),
        turn_id=turn_id,
        conversation_id=ConversationId(f"assistant:{turn_id}"),
        request={"prompt": "hello"},
        created_at=NOW,
    )
    activity = InferenceActivity(
        draft=draft,
        status=InferenceActivityStatus.PROCESSING,
        version=1,
        attempt_count=attempt_count,
        next_attempt_at=None,
        updated_at=NOW + timedelta(seconds=1),
    )
    return InferenceActivityClaim(
        activity=activity,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(minutes=1),
    )


class _Clock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed time.

        @return 测试时间 / Test time.
        """

        return NOW + timedelta(seconds=2)


class _Repository:
    """@brief 记录活动状态调用的 repository 替身 / Repository double recording activity state calls."""

    def __init__(self, claims: tuple[InferenceActivityClaim, ...] = ()) -> None:
        """@brief 创建替身 / Create the double.

        @param claims 首轮 claims / Claims available in the first poll.
        """

        self.claims = list(claims)
        self.claim_limits: list[int] = []
        self.completed: list[
            tuple[InferenceActivityClaim, MessageDraft, OutboundDraft, datetime]
        ] = []
        self.retried: list[tuple[InferenceActivityClaim, datetime, datetime, str]] = []
        self.failed: list[tuple[InferenceActivityClaim, datetime, str]] = []
        self.recover_calls = 0

    async def claim_inference_activities(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[InferenceActivityClaim, ...]:
        """@brief 按容量领取 claims / Claim up to capacity."""

        del now, lease_for
        self.claim_limits.append(limit)
        claimed = tuple(self.claims[:limit])
        del self.claims[:limit]
        return claimed

    async def complete_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbound: OutboundDraft,
        completed_at: datetime,
    ) -> object:
        """@brief 记录成功提交 / Record successful completion."""

        self.completed.append((claim, assistant_message, outbound, completed_at))
        return object()

    async def retry_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录重试 / Record retry."""

        self.retried.append((claim, failed_at, retry_at, error))

    async def fail_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录最终失败 / Record final failure."""

        self.failed.append((claim, failed_at, error))

    async def recover_expired_inference_leases(self, *, now: datetime) -> int:
        """@brief 记录恢复调用 / Record lease recovery."""

        del now
        self.recover_calls += 1
        return 0


class _Inference:
    """@brief 返回结果、异常或阻塞的推理端口替身 / Inference-port double returning, raising, or blocking."""

    def __init__(self, result: InferenceResult | Exception) -> None:
        """@brief 创建替身 / Create the double.

        @param result 固定结果或异常 / Fixed result or exception.
        """

        self.result = result
        self.started = 0
        self.release: asyncio.Event | None = None

    async def infer(self, request: dict[str, object]) -> InferenceResult:
        """@brief 返回、抛错或等待 / Return, raise, or wait.

        @param request 结构请求 / Structured request.
        @return 固定结果 / Fixed result.
        """

        assert request == {"prompt": "hello"}
        self.started += 1
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _result() -> InferenceResult:
    """@brief 构造推理结果 / Build an inference result.

    @return 类型化结果 / Typed result.
    """

    return InferenceResult(
        assistant_content={"text": "world"},
        outbound=InferenceOutboundIntent(
            delivery_stream_id=DeliveryStreamId("connector:stream:7"),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={"chat_id": 7, "text": "world"},
        ),
    )


def _worker(
    repository: _Repository,
    inference: _Inference,
    *,
    worker_count: int = 1,
    attempt_timeout: timedelta = timedelta(seconds=5),
) -> InferenceWorker:
    """@brief 构造测试 worker / Build a test worker."""

    return InferenceWorker(
        repository=repository,  # type: ignore[arg-type]
        inference=inference,  # type: ignore[arg-type]
        worker_count=worker_count,
        poll_interval=0.005,
        runtime_limits=InferenceRuntimeLimits(
            provider_timeout=min(timedelta(seconds=2), attempt_timeout / 2),
            attempt_timeout=attempt_timeout,
            lease_for=timedelta(seconds=30),
        ),
        retry_policy=FullJitterInferenceRetryPolicy(
            max_attempts=3,
            jitter=lambda lower, upper: upper,
        ),
        clock=_Clock(),
    )


def test_success_builds_deterministic_effects_and_completes_claim() -> None:
    """@brief 成功结果生成确定性历史与出站意图 / Success builds deterministic history and outbound intents."""

    async def scenario() -> None:
        claim = _claim()
        repository = _Repository()
        worker = _worker(repository, _Inference(_result()))
        await worker.process_claim(claim)
        assert len(repository.completed) == 1
        saved_claim, message, outbound, completed_at = repository.completed[0]
        assert saved_claim == claim
        assert message.content == {"text": "world"}
        assert outbound.payload == {"chat_id": 7, "text": "world"}
        assert completed_at == NOW + timedelta(seconds=2)

    asyncio.run(scenario())


def test_retryable_and_permanent_errors_follow_taxonomy() -> None:
    """@brief 错误 taxonomy 分流重试与最终失败 / Error taxonomy routes retry and final failure."""

    async def scenario() -> None:
        retry_repository = _Repository()
        retry_error = RetryableInferenceError(
            "busy",
            category=InferenceErrorCategory.RATE_LIMIT,
            retry_after=timedelta(seconds=7),
        )
        await _worker(retry_repository, _Inference(retry_error)).process_claim(_claim())
        assert len(retry_repository.retried) == 1
        assert retry_repository.retried[0][2] > NOW + timedelta(seconds=9)

        fail_repository = _Repository()
        permanent = PermanentInferenceError(
            "bad request",
            category=InferenceErrorCategory.INVALID_REQUEST,
        )
        await _worker(fail_repository, _Inference(permanent)).process_claim(_claim())
        assert len(fail_repository.failed) == 1

    asyncio.run(scenario())


def test_durable_dependency_wait_does_not_exhaust_provider_attempt_budget() -> None:
    """@brief Compaction gate 即使 claim 次数很高仍重试，由 dependency 终态负责收敛 / A compaction gate remains retryable at a high claim count and converges through the dependency's terminal state."""

    async def scenario() -> None:
        """@brief 执行超出普通 retry budget 的 dependency wait / Execute a dependency wait beyond the ordinary retry budget."""

        repository = _Repository()
        pending = InferenceDependencyPending(
            "compaction pending",
            retry_after=timedelta(seconds=5),
        )

        await _worker(repository, _Inference(pending)).process_claim(
            _claim(attempt_count=99)
        )

        assert len(repository.retried) == 1
        assert repository.failed == []
        assert repository.retried[0][2] > NOW + timedelta(seconds=7)

    asyncio.run(scenario())


def test_timeout_retries_but_task_cancellation_leaves_lease() -> None:
    """@brief attempt timeout 安排重试，外部取消保留租约 / Attempt timeout retries while external cancellation leaves the lease."""

    async def scenario() -> None:
        timeout_repository = _Repository()
        blocking = _Inference(_result())
        blocking.release = asyncio.Event()
        await _worker(
            timeout_repository,
            blocking,
            attempt_timeout=timedelta(milliseconds=1),
        ).process_claim(_claim())
        assert len(timeout_repository.retried) == 1
        assert "timeout" in timeout_repository.retried[0][3]

        cancelled_repository = _Repository()
        cancelled_port = _Inference(_result())
        cancelled_port.release = asyncio.Event()
        task = asyncio.create_task(
            _worker(cancelled_repository, cancelled_port).process_claim(_claim())
        )
        while cancelled_port.started == 0:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("inference cancellation was swallowed")
        assert not cancelled_repository.retried
        assert not cancelled_repository.failed
        assert not cancelled_repository.completed

    asyncio.run(scenario())


def test_task_group_capacity_and_graceful_shutdown_are_bounded() -> None:
    """@brief 固定 consumers 限制领取容量并在 shutdown drain / Fixed consumers bound claims and drain on shutdown."""

    async def scenario() -> None:
        claims = tuple(_claim() for _ in range(4))
        repository = _Repository(claims)
        inference = _Inference(_result())
        inference.release = asyncio.Event()
        worker = _worker(repository, inference, worker_count=2)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        while inference.started < 2:
            await asyncio.sleep(0.001)
        assert max(repository.claim_limits) == 2
        assert len(repository.claims) == 2
        stop.set()
        inference.release.set()
        await task
        assert len(repository.completed) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider", "attempt", "lease", "expected"),
    (
        (0, 5, 10, "provider_timeout must be positive"),
        (5, 5, 10, "provider_timeout must be shorter"),
        (6, 5, 10, "provider_timeout must be shorter"),
        (1, 5, 5, "attempt_timeout must be shorter"),
        (1, 6, 5, "attempt_timeout must be shorter"),
    ),
)
def test_runtime_limits_reject_unsafe_timeout_relationships(
    provider: int,
    attempt: int,
    lease: int,
    expected: str,
) -> None:
    """@brief 三层 timeout 必须严格递增 / Three timeout layers must be strictly increasing."""

    with pytest.raises(ValueError, match=expected):
        InferenceRuntimeLimits(
            provider_timeout=timedelta(seconds=provider),
            attempt_timeout=timedelta(seconds=attempt),
            lease_for=timedelta(seconds=lease),
        )
