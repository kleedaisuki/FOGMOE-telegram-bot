"""@brief 可恢复推理活动 worker 测试 / Tests for the recoverable inference-activity worker."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from observability_testkit import make_telemetry

import fogmoe_bot.application.conversation.inference_worker as inference_worker_module
from fogmoe_bot.application.conversation.inference_worker import (
    FullJitterInferenceRetryPolicy,
    InferenceDependencyPending,
    InferenceErrorCategory,
    InferenceOutboundIntent,
    InferenceResult,
    InferenceRuntimeLimits,
    InferenceWorker,
    PermanentInferenceError,
    RetryableInferenceError,
)
from fogmoe_bot.application.context_window.projection import (
    project_conversation_message,
)
from fogmoe_bot.application.assistant.streaming import (
    AssistantStreamAddress,
    AssistantStreamFrame,
    AssistantStreamSession,
    AssistantStreamState,
)
from fogmoe_bot.application.runtime import AdaptivePollingPolicy
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    InferenceActivityId,
    LeaseToken,
    MessageSequence,
    TurnId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityDraft,
    InferenceGenerationFence,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import ConversationMessage, MessageDraft
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)

NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _claim(
    *,
    attempt_count: int = 1,
    retry_budget_used: int = 0,
) -> InferenceActivityClaim:
    """@brief 构造 processing 推理 claim / Build a processing inference claim.

    @param attempt_count 已领取次数 / Recorded claim count.
    @param retry_budget_used 已持久化普通失败预算 / Persisted ordinary failure budget.
    @return 测试 claim / Test claim.
    """

    turn_id = TurnId.new()
    draft = InferenceActivityDraft(
        activity_id=InferenceActivityId.for_turn(turn_id),
        turn_id=turn_id,
        conversation_id=ConversationId(f"assistant:{turn_id}"),
        request={
            "prompt": "hello",
            "task_kind": "assistant",
            "delivery_stream_id": "connector:stream:7",
            "chat_id": 7,
            "reply_to_message_id": 11,
            "message_thread_id": None,
            "disable_notification": False,
            "protect_content": False,
            "disable_web_page_preview": True,
        },
        created_at=NOW,
    )
    activity = InferenceActivity(
        draft=draft,
        status=InferenceActivityStatus.PROCESSING,
        version=1,
        attempt_count=attempt_count,
        retry_budget_used=retry_budget_used,
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

    def __init__(
        self,
        claims: tuple[InferenceActivityClaim, ...] = (),
        *,
        recovery_failures: int = 0,
    ) -> None:
        """@brief 创建替身 / Create the double.

        @param claims 首轮 claims / Claims available in the first poll.
        """

        self.claims = list(claims)
        self.claim_limits: list[int] = []
        self.completed: list[
            tuple[InferenceActivityClaim, tuple[OutboundDraft, ...], datetime]
        ] = []
        self.retried: list[tuple[InferenceActivityClaim, datetime, datetime, str]] = []
        self.retry_budgets: list[int] = []
        """@brief 每次 retry 提交的绝对预算值 / Absolute budget value committed by each retry."""
        self.failed: list[
            tuple[
                InferenceActivityClaim,
                MessageDraft,
                tuple[OutboundDraft, ...],
                datetime,
                str,
            ]
        ] = []
        self.failure_budgets: list[int] = []
        """@brief 每次 final failure 提交的绝对预算值 / Absolute budget value committed by each final failure."""
        self.recover_calls = 0
        self.recovery_failures = recovery_failures

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
        outbounds: tuple[OutboundDraft, ...],
        completed_at: datetime,
    ) -> object:
        """@brief 记录成功提交 / Record successful completion."""

        del assistant_message
        self.completed.append((claim, outbounds, completed_at))
        return object()

    async def retry_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> None:
        """@brief 记录重试 / Record retry."""

        self.retried.append((claim, failed_at, retry_at, error))
        self.retry_budgets.append(retry_budget_used)

    async def fail_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: tuple[OutboundDraft, ...],
        failed_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> object:
        """@brief 记录最终失败与安全反馈 / Record final failure and safe feedback."""

        self.failed.append(
            (claim, assistant_message, outbounds, failed_at, error)
        )
        self.failure_budgets.append(retry_budget_used)
        return object()

    async def recover_expired_inference_leases(self, *, now: datetime) -> int:
        """@brief 记录恢复调用 / Record lease recovery."""

        del now
        self.recover_calls += 1
        if self.recovery_failures:
            self.recovery_failures -= 1
            raise OSError("temporary inference lease-recovery failure")
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

    async def infer(
        self,
        request: JsonObject,
        *,
        execution_deadline_monotonic: float | None = None,
        generation_fence: InferenceGenerationFence | None = None,
        stream: AssistantStreamSession | None = None,
    ) -> InferenceResult:
        """@brief 返回、抛错或等待 / Return, raise, or wait.

        @param request 结构请求 / Structured request.
        @param execution_deadline_monotonic worker 建立的 attempt 单调截止点 /
            Attempt monotonic deadline established by the worker.
        @param generation_fence 当前 processing claim 的 generation fence /
            Generation fence of the current processing claim.
        @param stream 可选易失流会话 / Optional ephemeral stream session.
        @return 固定结果 / Fixed result.
        """

        del stream
        assert request["prompt"] == "hello"
        assert execution_deadline_monotonic is None or execution_deadline_monotonic > 0.0
        assert generation_fence is not None
        self.started += 1
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _StreamStarter:
    """@brief 记录 worker 所有流帧的 generation starter / Generation starter recording every worker-owned stream frame."""

    def __init__(self, order: list[str]) -> None:
        """@brief 保存共享 durable/stream 顺序日志 / Store a shared durable/stream ordering log.

        @param order 共享事件日志 / Shared event log.
        """

        self.order = order
        self.frames: list[AssistantStreamFrame] = []

    async def start_stream(
        self,
        request: JsonObject,
        *,
        generation_fence: InferenceGenerationFence | None = None,
    ) -> AssistantStreamSession:
        """@brief 建立并投影首帧 / Build and project the first frame.

        @param request durable request / Durable request.
        @param generation_fence 当前 claim fence / Current claim fence.
        @return 测试流会话 / Test stream session.
        """

        assert generation_fence is not None
        chat_id = request["chat_id"]
        assert isinstance(chat_id, int) and not isinstance(chat_id, bool)
        state = AssistantStreamState.begin(
            turn_id=generation_fence.turn_id,
            address=AssistantStreamAddress(
                chat_id=chat_id,
                is_group=False,
                message_thread_id=None,
            ),
            generation=generation_fence.attempt,
            revision=int(generation_fence.input_revision),
            emitted_at=NOW,
        )
        session = AssistantStreamSession(state=state, projection=self)
        await session.start()
        return session

    async def project(self, frame: AssistantStreamFrame) -> None:
        """@brief 记录流帧 / Record a stream frame.

        @param frame 当前流帧 / Current stream frame.
        @return None / None.
        """

        self.frames.append(frame)
        self.order.append(f"stream:{frame.kind.value}")


class _OrderedRepository(_Repository):
    """@brief 记录 durable 提交与流帧相对顺序的 repository / Repository recording durable commits relative to stream frames."""

    def __init__(
        self,
        order: list[str],
        *,
        reject_completion: bool = False,
    ) -> None:
        """@brief 创建顺序记录器 / Create the ordering recorder.

        @param order 共享事件日志 / Shared event log.
        @param reject_completion 是否模拟 completion 事务失败 /
            Whether to simulate a failed completion transaction.
        """

        super().__init__()
        self.order = order
        self.reject_completion = reject_completion

    async def complete_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: tuple[OutboundDraft, ...],
        completed_at: datetime,
    ) -> object:
        """@brief 提交成功或模拟回滚 / Commit success or simulate rollback."""

        self.order.append("durable:complete")
        if self.reject_completion:
            raise OSError("database unavailable")
        return await super().complete_inference_activity(
            claim,
            assistant_message=assistant_message,
            outbounds=outbounds,
            completed_at=completed_at,
        )

    async def retry_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> None:
        """@brief 记录 durable retry 提交 / Record the durable retry commit."""

        self.order.append("durable:retry")
        await super().retry_inference_activity(
            claim,
            failed_at=failed_at,
            retry_at=retry_at,
            error=error,
            retry_budget_used=retry_budget_used,
        )

    async def fail_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: tuple[OutboundDraft, ...],
        failed_at: datetime,
        error: str,
        retry_budget_used: int,
    ) -> object:
        """@brief 记录 durable final-failure 提交 / Record the durable final-failure commit."""

        self.order.append("durable:fail")
        return await super().fail_inference_activity(
            claim,
            assistant_message=assistant_message,
            outbounds=outbounds,
            failed_at=failed_at,
            error=error,
            retry_budget_used=retry_budget_used,
        )


def _result() -> InferenceResult:
    """@brief 构造推理结果 / Build an inference result.

    @return 类型化结果 / Typed result.
    """

    return InferenceResult(
        assistant_content={"text": "world"},
        outbounds=(
            InferenceOutboundIntent(
                delivery_stream_id=DeliveryStreamId("connector:stream:7"),
                kind=SEND_TELEGRAM_MESSAGE,
                payload={"chat_id": 7, "text": "world"},
            ),
        ),
    )


def _worker(
    repository: _Repository,
    inference: _Inference,
    *,
    streaming: _StreamStarter | None = None,
    worker_count: int = 1,
    attempt_timeout: timedelta = timedelta(seconds=5),
    lease_for: timedelta = timedelta(seconds=30),
    polling_policy: AdaptivePollingPolicy | None = None,
) -> InferenceWorker:
    """@brief 构造测试 worker / Build a test worker."""

    return InferenceWorker(
        repository=repository,  # type: ignore[arg-type]
        inference=inference,  # type: ignore[arg-type]
        streaming=streaming,
        worker_count=worker_count,
        polling_policy=polling_policy
        or AdaptivePollingPolicy(0.005, 0.01, jitter_ratio=0.0),
        runtime_limits=InferenceRuntimeLimits(
            provider_timeout=min(timedelta(seconds=2), attempt_timeout / 2),
            attempt_timeout=attempt_timeout,
            lease_for=lease_for,
        ),
        retry_policy=FullJitterInferenceRetryPolicy(
            max_attempts=3,
            jitter=lambda lower, upper: upper,
        ),
        clock=_Clock(),
        telemetry=make_telemetry(),
    )


def test_success_builds_deterministic_effects_and_completes_claim() -> None:
    """@brief 成功结果生成确定性历史与出站意图 / Success builds deterministic history and outbound intents."""

    async def scenario() -> None:
        claim = _claim()
        repository = _Repository()
        worker = _worker(repository, _Inference(_result()))
        await worker.process_claim(claim)
        assert len(repository.completed) == 1
        saved_claim, outbounds, completed_at = repository.completed[0]
        assert saved_claim == claim
        assert len(outbounds) == 1
        assert outbounds[0].payload == {"chat_id": 7, "text": "world"}
        assert completed_at == NOW + timedelta(seconds=2)

    asyncio.run(scenario())


def test_stream_terminals_follow_the_committed_durable_decision() -> None:
    """@brief COMPLETED/FAILED/SUSPENDED 只在对应 durable 事务之后投影 /
    COMPLETED, FAILED, and SUSPENDED are projected only after their durable transaction.
    """

    async def scenario() -> None:
        """@brief 覆盖成功、重试、重试耗尽与提交失败 / Cover success, retry, exhausted retry, and commit failure."""

        success_order: list[str] = []
        success_repository = _OrderedRepository(success_order)
        success_stream = _StreamStarter(success_order)
        await _worker(
            success_repository,
            _Inference(_result()),
            streaming=success_stream,
        ).process_claim(_claim())
        assert success_order == [
            "stream:started",
            "durable:complete",
            "stream:completed",
        ]

        retry_order: list[str] = []
        retry_repository = _OrderedRepository(retry_order)
        retry_stream = _StreamStarter(retry_order)
        retry_error = RetryableInferenceError(
            "busy",
            category=InferenceErrorCategory.RATE_LIMIT,
        )
        await _worker(
            retry_repository,
            _Inference(retry_error),
            streaming=retry_stream,
        ).process_claim(_claim())
        assert retry_order == [
            "stream:started",
            "durable:retry",
            "stream:suspended",
        ]

        exhausted_order: list[str] = []
        exhausted_repository = _OrderedRepository(exhausted_order)
        exhausted_stream = _StreamStarter(exhausted_order)
        await _worker(
            exhausted_repository,
            _Inference(retry_error),
            streaming=exhausted_stream,
        ).process_claim(_claim(attempt_count=99, retry_budget_used=2))
        assert exhausted_order == [
            "stream:started",
            "durable:fail",
            "stream:failed",
        ]

        rollback_order: list[str] = []
        rollback_repository = _OrderedRepository(
            rollback_order,
            reject_completion=True,
        )
        rollback_stream = _StreamStarter(rollback_order)
        with pytest.raises(OSError, match="database unavailable"):
            await _worker(
                rollback_repository,
                _Inference(_result()),
                streaming=rollback_stream,
            ).process_claim(_claim())
        assert rollback_order == [
            "stream:started",
            "durable:complete",
            "stream:suspended",
        ]

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
        assert retry_repository.retry_budgets == [1]
        assert retry_repository.retried[0][2] > NOW + timedelta(seconds=9)

        fail_repository = _Repository()
        permanent = PermanentInferenceError(
            "bad request",
            category=InferenceErrorCategory.INVALID_REQUEST,
        )
        await _worker(fail_repository, _Inference(permanent)).process_claim(_claim())
        assert len(fail_repository.failed) == 1
        _, failure_message, failure_outbounds, _, internal_error = (
            fail_repository.failed[0]
        )
        safe_text = failure_message.content["text"]
        assert isinstance(safe_text, str)
        assert "invalid_request" in safe_text
        assert "bad request" not in safe_text
        assert failure_outbounds[0].payload["text"] == safe_text
        assert "bad request" not in str(failure_outbounds[0].payload)
        assert "bad request" in internal_error
        assert fail_repository.failure_budgets == [1]
        projected = project_conversation_message(
            ConversationMessage(
                draft=failure_message,
                sequence=MessageSequence(1),
            )
        )
        assert [message.text for message in projected] == [safe_text]

    asyncio.run(scenario())


def test_durable_dependency_wait_does_not_exhaust_ordinary_retry_budget() -> None:
    """@brief Compaction gate 即使 claim 次数很高仍不消耗普通重试预算 /
    A compaction gate does not consume ordinary retry budget even at a high claim count.
    """

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
        assert repository.retry_budgets == [0]
        assert repository.retried[0][2] > NOW + timedelta(seconds=7)

    asyncio.run(scenario())


def test_first_ordinary_failure_after_many_dependency_claims_has_a_fresh_budget() -> None:
    """@brief 多次 dependency claim 后首个普通失败仍是预算一 /
    The first ordinary failure after many dependency claims still consumes only budget one.
    """

    async def scenario() -> None:
        """@brief 用独立 claim 与预算计数验证语义 / Verify semantics with independent claim and budget counters."""

        repository = _Repository()
        transient = RetryableInferenceError(
            "provider busy",
            category=InferenceErrorCategory.PROVIDER_UNAVAILABLE,
        )

        await _worker(repository, _Inference(transient)).process_claim(
            _claim(attempt_count=100, retry_budget_used=0)
        )

        assert repository.failed == []
        assert len(repository.retried) == 1
        assert repository.retry_budgets == [1]

    asyncio.run(scenario())


def test_superseded_claim_is_an_informational_fencing_outcome(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief reset 使 claim 失效时不把正常 fencing 记录为错误 / A reset-superseded claim is not logged as an error.

    @param caplog pytest 日志捕获器 / pytest log capture fixture.
    @param monkeypatch pytest 属性替换器 / pytest attribute patcher.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 模拟外部推理期间发生的会话 reset / Simulate a conversation reset during external inference.

        @return None / None.
        """

        repository = _Repository()

        async def stale_completion(
            claim: InferenceActivityClaim,
            *,
            assistant_message: MessageDraft,
            outbounds: tuple[OutboundDraft, ...],
            completed_at: datetime,
        ) -> object:
            """@brief 模拟 reset 失效的 completion claim / Simulate a reset-invalidated completion claim.

            @param claim 已被 fencing 的 claim / Claim invalidated by fencing.
            @param assistant_message 待提交的助手消息 / Assistant message pending commit.
            @param outbounds 待提交的出站效果 / Outbound effects pending commit.
            @param completed_at 推理完成时间 / Inference completion time.
            @return 永不返回 / Never returns.
            """

            del claim, assistant_message, outbounds, completed_at
            raise StaleClaimError("claim cancelled by conversation reset")

        monkeypatch.setattr(repository, "complete_inference_activity", stale_completion)
        worker = _worker(repository, _Inference(_result()))
        queue: asyncio.Queue[inference_worker_module._WorkItem] = asyncio.Queue()
        capacity: asyncio.Queue[None] = asyncio.Queue()
        claim = _claim()
        await queue.put(inference_worker_module._ClaimWork(claim))
        await queue.put(inference_worker_module._StopConsumer())

        with caplog.at_level(logging.INFO, logger=inference_worker_module.__name__):
            await worker._consume(queue, capacity)

        assert capacity.qsize() == 1
        records = [
            record
            for record in caplog.records
            if "superseded before finalization" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert getattr(records[0], "event_name", None) == "inference.claim.superseded"

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
        assert repository.recover_calls == 1
        stop.set()
        inference.release.set()
        await task
        assert len(repository.completed) == 2

    asyncio.run(scenario())


def test_lease_recovery_survives_failure_and_saturated_capacity() -> None:
    """@brief 恢复故障不阻断推理 claim，容量饱和也不暂停 cadence / Recovery failure does not block inference claims, and saturated capacity does not pause the cadence."""

    async def scenario() -> None:
        """@brief 阻塞唯一推理 consumer 并等待第二次恢复 / Block the sole inference consumer and await a second recovery pass."""

        repository = _Repository((_claim(),), recovery_failures=1)
        inference = _Inference(_result())
        inference.release = asyncio.Event()
        worker = _worker(
            repository,
            inference,
            attempt_timeout=timedelta(milliseconds=90),
            lease_for=timedelta(milliseconds=100),
            polling_policy=AdaptivePollingPolicy(
                0.001,
                0.002,
                jitter_ratio=0.0,
            ),
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run(stop_event))

        async with asyncio.timeout(1):
            while not inference.started or repository.recover_calls < 2:
                await asyncio.sleep(0.001)

        assert repository.claim_limits == [1]
        assert repository.recover_calls >= 2

        stop_event.set()
        inference.release.set()
        await asyncio.wait_for(task, timeout=1)

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
