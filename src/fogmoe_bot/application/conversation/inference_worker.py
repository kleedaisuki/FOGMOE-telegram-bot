"""@brief Provider-neutral 可恢复推理活动 worker / Provider-neutral recoverable inference-activity worker.

外部推理 I/O 只发生在 repository 事务之外。固定数量的 ``TaskGroup`` consumer 与
容量令牌共同限制“已领取但未终结”的活动数；取消不会清理数据库 claim，而是保留
租约供其他实例在到期后恢复。/ External inference I/O occurs only outside repository
transactions. Fixed ``TaskGroup`` consumers and capacity tokens bound claimed-but-unfinalized
activities. Cancellation deliberately leaves the database claim leased for recovery by another
instance after expiry.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    Jitter,
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.identity import (
    ConversationMessageId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.inference import InferenceActivityClaim
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundKind,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.workflow_results import InferenceCompletionResult
from fogmoe_bot.domain.observability.conventions import EventName, MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus
from fogmoe_bot.domain.temporal import ensure_utc

logger = logging.getLogger(__name__)


class InferencePersistence(Protocol):
    """@brief 推理 worker 所需最小持久化端口 / Minimal persistence port for the inference worker."""

    async def claim_inference_activities(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[InferenceActivityClaim]:
        """@brief 原子领取可执行活动 / Atomically claim runnable activities.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum claim count.
        @param lease_for fencing 租约时长 / Fencing lease duration.
        @return 活动 claims / Activity claims.
        """

        ...

    async def complete_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
        completed_at: datetime,
    ) -> InferenceCompletionResult:
        """@brief 原子完成活动、历史与 outbox / Atomically complete the activity, history, and outbox.

        @param claim 当前 claim / Current claim.
        @param assistant_message 确定性助手消息 / Deterministic assistant message.
        @param outbounds 有序、确定性的出站意图 / Ordered deterministic outbound intents.
        @param completed_at 完成时间 / Completion time.
        @return 完成回执 / Completion receipt.
        """

        ...

    async def retry_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 原子安排活动与 Turn 重试 / Atomically schedule activity and Turn retry.

        @param claim 当前 claim / Current claim.
        @param failed_at 失败时间 / Failure time.
        @param retry_at 下次领取时间 / Next claim time.
        @param error 错误摘要 / Error summary.
        @return None / None.
        """

        ...

    async def fail_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 原子终结活动与 Turn / Atomically fail activity and Turn finally.

        @param claim 当前 claim / Current claim.
        @param failed_at 最终失败时间 / Final-failure time.
        @param error 错误摘要 / Error summary.
        @return None / None.
        """

        ...

    async def recover_expired_inference_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃或取消留下的过期租约 / Recover leases left by crashes or cancellation.

        @param now 当前 UTC 时间 / Current UTC time.
        @return 回收数量 / Number recovered.
        """

        ...


@dataclass(frozen=True, slots=True)
class InferenceOutboundIntent:
    """@brief 推理结果携带的类型化出站意图 / Typed outbound intent carried by an inference result.

    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param kind 可扩展动作 kind / Extensible action kind.
    @param payload connector-neutral 结构载荷 / Connector-neutral structured payload.
    """

    delivery_stream_id: DeliveryStreamId
    kind: OutboundKind
    payload: JsonObject

    def __post_init__(self) -> None:
        """@brief 隔离可变 payload / Isolate the mutable payload.

        @return None / None.
        """

        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """@brief Provider-neutral 推理成功结果 / Provider-neutral successful inference result.

    @param assistant_content 结构化助手历史内容 / Structured assistant-history content.
    @param outbounds 一次发送的有序出站意图 / Ordered outbound intents for one delivery.
    """

    assistant_content: JsonObject
    outbounds: tuple[InferenceOutboundIntent, ...]

    def __post_init__(self) -> None:
        """@brief 隔离可变助手内容 / Isolate mutable assistant content.

        @return None / None.
        """

        object.__setattr__(self, "assistant_content", dict(self.assistant_content))
        if not self.outbounds:
            raise ValueError("Inference results require at least one outbound intent")
        first_stream = self.outbounds[0].delivery_stream_id
        if any(intent.delivery_stream_id != first_stream for intent in self.outbounds):
            raise ValueError(
                "Inference delivery intents must share one delivery stream"
            )


class InferencePort(Protocol):
    """@brief 单次 provider-neutral 推理端口 / Port for one provider-neutral inference attempt."""

    async def infer(self, request: JsonObject) -> InferenceResult:
        """@brief 执行一次外部推理尝试 / Perform one external inference attempt.

        @param request durable provider-neutral 请求 / Durable provider-neutral request.
        @return 类型化推理结果 / Typed inference result.
        @note 实现不得吞掉 CancelledError，也不得自行写 conversation 表。/
        Implementations must not swallow CancelledError or write conversation tables themselves.
        """

        ...


class InferenceErrorCategory(StrEnum):
    """@brief 可持久化推理错误分类 / Persistable inference-error category."""

    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    INVALID_OUTPUT = "invalid_output"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CONFIGURATION = "configuration"
    CONTEXT_WINDOW = "context_window"
    SAFETY = "safety"
    PARTIAL_EFFECT = "partial_effect"
    INTERNAL = "internal"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER = "provider"


class InferenceError(RuntimeError):
    """@brief 已分类推理错误 / Classified inference error.

    @param message 错误详情 / Error detail.
    @param category 稳定错误分类 / Stable error category.
    """

    category: InferenceErrorCategory

    def __init__(
        self,
        message: str,
        *,
        category: InferenceErrorCategory,
    ) -> None:
        """@brief 创建分类错误 / Create a classified error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        """

        super().__init__(message)
        self.category = category


class RetryableInferenceError(InferenceError):
    """@brief 可在预算内重试的推理错误 / Inference error retryable within budget.

    @param retry_after Provider 指定的最小等待 / Provider-specified minimum delay.
    """

    retry_after: timedelta | None

    def __init__(
        self,
        message: str,
        *,
        category: InferenceErrorCategory,
        retry_after: timedelta | None = None,
    ) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        @param retry_after Provider 最小等待 / Provider minimum delay.
        @raise ValueError retry_after 非正时抛出 / Raised for a non-positive retry_after.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("retry_after must be positive")
        super().__init__(message, category=category)
        self.retry_after = retry_after


class InferenceDependencyPending(RetryableInferenceError):
    """@brief 等待另一个 durable activity 的非计费重试信号 / Retry signal that waits for another durable activity without consuming the provider-attempt budget.

    @param retry_after 再检查 dependency 的等待 / Delay before checking the dependency again.
    @note dependency 自身必须拥有 retry/fallback/final 状态机；一旦进入终态，下一次
    inference projection 会成功或返回 permanent error。/ The dependency must own its own
    retry/fallback/final state machine; once terminal, the next inference projection either
    succeeds or returns a permanent error.
    """

    def __init__(self, message: str, *, retry_after: timedelta) -> None:
        """@brief 创建 durable dependency gate / Create a durable-dependency gate.

        @param message dependency detail / Dependency detail.
        @param retry_after 正等待 / Positive wait.
        """

        super().__init__(
            message,
            category=InferenceErrorCategory.CONTEXT_WINDOW,
            retry_after=retry_after,
        )


class InferenceAttemptTimeout(RetryableInferenceError):
    """@brief Worker 强制终止的推理超时 / Inference timeout enforced by the worker."""

    def __init__(self, message: str) -> None:
        """@brief 创建超时错误 / Create a timeout error.

        @param message 超时详情 / Timeout detail.
        """

        super().__init__(message, category=InferenceErrorCategory.TIMEOUT)


class PermanentInferenceError(InferenceError):
    """@brief 不应自动重试的推理错误 / Inference error that must not be retried automatically."""


class InferenceOutputError(PermanentInferenceError):
    """@brief Provider 返回不合法结构 / Provider returned an invalid structured result."""

    def __init__(self, message: str) -> None:
        """@brief 创建输出错误 / Create an output error.

        @param message 错误详情 / Error detail.
        """

        super().__init__(message, category=InferenceErrorCategory.INVALID_OUTPUT)


@dataclass(frozen=True, slots=True)
class RetryInferenceAt:
    """@brief 在指定时刻重试推理 / Retry inference at a specified time.

    @param at 下次可领取时间 / Next claimable time.
    """

    at: datetime


@dataclass(frozen=True, slots=True)
class FailInferenceFinal:
    """@brief 将推理活动标记永久失败 / Mark an inference activity finally failed."""


type InferenceFailureDecision = RetryInferenceAt | FailInferenceFinal
"""@brief 推理失败的穷尽策略决定 / Exhaustive inference-failure decision."""


@dataclass(frozen=True, slots=True)
class FullJitterInferenceRetryPolicy:
    """@brief 指数退避、Full Jitter 与 Retry-After 策略 / Exponential-backoff, Full-Jitter, and Retry-After policy.

    @param max_attempts 包含首次 claim 的最大尝试数 / Maximum attempts including the first claim.
    @param initial_delay 第一次重试指数上限 / Exponential cap for the first retry.
    @param max_delay 最大指数上限 / Maximum exponential cap.
    @param retry_after_jitter Provider 延迟后的最大附加抖动 / Maximum jitter added after provider delay.
    @param jitter 可注入随机源 / Injectable random source.
    """

    max_attempts: int = 8
    initial_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    retry_after_jitter: timedelta = timedelta(seconds=1)
    jitter: Jitter = random.uniform

    def __post_init__(self) -> None:
        """@brief 校验策略参数 / Validate policy parameters.

        @return None / None.
        @raise ValueError 次数或延迟非法时抛出 / Raised for invalid attempts or delays.
        """

        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay <= timedelta():
            raise ValueError("initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay cannot be smaller than initial_delay")
        if self.retry_after_jitter < timedelta():
            raise ValueError("retry_after_jitter cannot be negative")

    def decide(
        self,
        *,
        attempt_count: int,
        failed_at: datetime,
        error: Exception,
    ) -> InferenceFailureDecision:
        """@brief 决定重试时间或永久失败 / Decide a retry time or final failure.

        @param attempt_count Repository 已记录领取次数 / Claim count recorded by the repository.
        @param failed_at 本次失败时间 / Failure time.
        @param error 推理异常 / Inference exception.
        @return 重试或永久失败决定 / Retry or final-failure decision.
        """

        failure_time = ensure_utc(failed_at)
        if isinstance(error, PermanentInferenceError | ValueError | TypeError):
            return FailInferenceFinal()
        if isinstance(error, InferenceDependencyPending):
            retry_after = error.retry_after
            if retry_after is None:  # pragma: no cover - constructor requires it.
                raise RuntimeError("Inference dependency gate lost its retry delay")
            provider_seconds = retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            return RetryInferenceAt(
                failure_time
                + retry_after
                + timedelta(seconds=self._sample(0.0, jitter_cap))
            )
        if attempt_count >= self.max_attempts:
            return FailInferenceFinal()
        if isinstance(error, RetryableInferenceError) and error.retry_after is not None:
            provider_seconds = error.retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            return RetryInferenceAt(
                failure_time
                + error.retry_after
                + timedelta(seconds=self._sample(0.0, jitter_cap))
            )
        exponent = max(0, attempt_count - 1)
        cap_seconds = min(
            self.max_delay.total_seconds(),
            self.initial_delay.total_seconds() * (2**exponent),
        )
        delay = timedelta(seconds=self._sample(0.0, cap_seconds))
        return RetryInferenceAt(
            failure_time + (delay if delay > timedelta() else timedelta.resolution)
        )

    def _sample(self, lower: float, upper: float) -> float:
        """@brief 验证并返回 jitter 样本 / Validate and return a jitter sample.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 合法样本秒数 / Valid sample in seconds.
        @raise ValueError 随机源越界或非有限时抛出 / Raised for out-of-range or non-finite samples.
        """

        value: float = self.jitter(lower, upper)
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("jitter returned a value outside its requested interval")
        return value


@dataclass(frozen=True, slots=True)
class _ClaimWork:
    """@brief consumer queue 中的 claim / Claim in the consumer queue.

    @param claim 待推理 claim / Claim to infer.
    """

    claim: InferenceActivityClaim


@dataclass(frozen=True, slots=True)
class _StopConsumer:
    """@brief consumer 正常 drain 后的停止哨兵 / Stop sentinel after normal consumer drain."""


type _WorkItem = _ClaimWork | _StopConsumer
"""@brief 推理 consumer 工作项 / Inference-consumer work item."""


@dataclass(frozen=True, slots=True)
class InferenceRuntimeLimits:
    """@brief 推理各层严格递增的超时预算 / Strictly increasing timeout budgets for inference layers.

    @param provider_timeout 单次 provider 请求预算 / Per-provider request budget.
    @param attempt_timeout worker 整体推理尝试预算 / Whole worker-attempt budget.
    @param lease_for 数据库 claim 租约预算 / Database-claim lease budget.
    @note 必须满足 ``provider_timeout < attempt_timeout < lease_for``，从而先由最内层
    provider 收敛，再由 worker 取消整次尝试，最后才允许另一实例回收 lease。/
    ``provider_timeout < attempt_timeout < lease_for`` is required so the innermost provider
    converges first, the worker cancels the whole attempt second, and only then may another
    instance recover the lease.
    """

    provider_timeout: timedelta
    attempt_timeout: timedelta
    lease_for: timedelta

    def __post_init__(self) -> None:
        """@brief 校验超时层级不变量 / Validate the timeout-layer invariant.

        @return None / None.
        @raise ValueError 任一预算非正或顺序不安全时抛出 / Raised when a budget is non-positive or unsafely ordered.
        """

        if self.provider_timeout <= timedelta():
            raise ValueError("provider_timeout must be positive")
        if self.attempt_timeout <= timedelta():
            raise ValueError("attempt_timeout must be positive")
        if self.lease_for <= timedelta():
            raise ValueError("lease_for must be positive")
        if self.provider_timeout >= self.attempt_timeout:
            raise ValueError("provider_timeout must be shorter than attempt_timeout")
        if self.attempt_timeout >= self.lease_for:
            raise ValueError("attempt_timeout must be shorter than lease_for")


class InferenceWorker:
    """@brief 有界领取、执行并终结推理活动 / Bounded worker claiming, executing, and finalizing inference activities."""

    def __init__(
        self,
        *,
        repository: InferencePersistence,
        inference: InferencePort,
        worker_count: int,
        polling_policy: AdaptivePollingPolicy,
        runtime_limits: InferenceRuntimeLimits,
        retry_policy: FullJitterInferenceRetryPolicy | None = None,
        clock: UtcClock | None = None,
        telemetry: Telemetry,
    ) -> None:
        """@brief 创建推理 worker / Create an inference worker.

        @param repository 活动持久化端口 / Activity persistence port.
        @param inference 外部推理端口 / External inference port.
        @param worker_count 已领取未终结活动上限 / Maximum claimed-but-unfinalized activities.
        @param polling_policy 自适应空闲轮询策略 / Adaptive idle-polling policy.
        @param runtime_limits provider、attempt 与 lease 的统一预算 / Shared provider, attempt, and lease budgets.
        @param retry_policy 失败策略 / Failure policy.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @return None / None.
        @raise ValueError 容量或时间参数非法时抛出 / Raised for invalid capacity or timing parameters.
        """

        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        self._repository = repository
        self._inference = inference
        self._worker_count = worker_count
        self._polling_policy = polling_policy
        self._lease_for = runtime_limits.lease_for
        self._attempt_timeout = runtime_limits.attempt_timeout
        self._retry_policy = retry_policy or FullJitterInferenceRetryPolicy()
        self._clock = clock or SystemUtcClock()
        self._telemetry = telemetry

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行 producer 与固定 consumers / Run one producer and fixed consumers.

        @param stop_event 置位后停止领取并 drain / Stops claiming and drains when set.
        @return None / None.
        @note 正常 shutdown 会 drain；Task 取消立即传播并保留 processing lease。/
        Normal shutdown drains; task cancellation propagates and leaves the processing lease.
        """

        work_queue: asyncio.Queue[_WorkItem] = asyncio.Queue(maxsize=self._worker_count)
        capacity: asyncio.Queue[None] = asyncio.Queue(maxsize=self._worker_count)
        for _ in range(self._worker_count):
            capacity.put_nowait(None)
        async with asyncio.TaskGroup() as task_group:
            for index in range(self._worker_count):
                task_group.create_task(
                    self._consume(work_queue, capacity),
                    name=f"inference-consumer-{index}",
                )
            try:
                await self._produce(work_queue, capacity, stop_event)
            except asyncio.CancelledError:
                stop_event.set()
                raise
            else:
                await work_queue.join()
                for _ in range(self._worker_count):
                    await work_queue.put(_StopConsumer())

    async def process_claim(self, claim: InferenceActivityClaim) -> None:
        """@brief 在事务外推理并以 fencing token 终结 / Infer outside a transaction and finalize with a fencing token.

        @param claim 当前活动 claim / Current activity claim.
        @return None / None.
        @note CancelledError 不被捕获；claim 保持 processing 直至租约恢复。/
        CancelledError is not caught; the claim remains processing until lease recovery.
        """

        activity = claim.activity
        with self._telemetry.span(
            "inference.attempt",
            kind=SpanKind.CONSUMER,
            parent=activity.draft.trace_context,
            attributes={
                "fogmoe.turn.id": str(activity.turn_id),
                "fogmoe.activity.id": str(activity.activity_id),
                "fogmoe.inference.attempt": activity.attempt_count,
            },
        ) as span:
            try:
                async with asyncio.timeout(self._attempt_timeout.total_seconds()):
                    result = await self._inference.infer(dict(activity.request))
            except TimeoutError:
                error = InferenceAttemptTimeout(
                    f"inference attempt exceeded {self._attempt_timeout.total_seconds():g}s"
                )
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                self._telemetry.counter(
                    MetricName.INFERENCE_OUTCOMES,
                    attributes={"outcome": Outcome.TIMEOUT},
                )
                await self._finalize_failure(claim, error)
                return
            except Exception as error:
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                self._telemetry.counter(
                    MetricName.INFERENCE_OUTCOMES,
                    attributes={"outcome": Outcome.FAILURE},
                )
                await self._finalize_failure(claim, error)
                return

            completed_at = self._clock.now()
            assistant_message = MessageDraft(
                message_id=ConversationMessageId.for_turn(
                    activity.turn_id,
                    "assistant.final",
                ),
                conversation_id=activity.conversation_id,
                turn_id=activity.turn_id,
                source_update_id=None,
                role=MessageRole.ASSISTANT,
                content=result.assistant_content,
                idempotency_key=f"turn:{activity.turn_id}:assistant:final",
                created_at=completed_at,
            )
            outbounds = tuple(
                OutboundDraft(
                    message_id=OutboundMessageId.for_turn(
                        activity.turn_id,
                        f"outbound.{ordinal}",
                    ),
                    conversation_id=activity.conversation_id,
                    turn_id=activity.turn_id,
                    delivery_stream_id=intent.delivery_stream_id,
                    kind=intent.kind,
                    payload=intent.payload,
                    idempotency_key=(f"turn:{activity.turn_id}:outbound:{ordinal}"),
                    created_at=completed_at,
                    trace_context=span.context,
                )
                for ordinal, intent in enumerate(result.outbounds)
            )
            await self._repository.complete_inference_activity(
                claim,
                assistant_message=assistant_message,
                outbounds=outbounds,
                completed_at=completed_at,
            )
            self._telemetry.counter(
                MetricName.INFERENCE_OUTCOMES,
                attributes={"outcome": Outcome.SUCCESS},
            )

    async def _produce(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 按容量回收租约并领取活动 / Recover leases and claim activities up to capacity.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity 空闲容量令牌 / Free-capacity tokens.
        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        polling = self._polling_policy.start()
        recovery = LeaseRecoveryCadence.for_lease(self._lease_for)
        while not stop_event.is_set():
            if recovery.take_due():
                await self._recover_expired_leases(self._clock.now())
            tokens = self._take_available(capacity)
            if tokens:
                now = self._clock.now()
                try:
                    claims = tuple(
                        await self._repository.claim_inference_activities(
                            now=now,
                            limit=len(tokens),
                            lease_for=self._lease_for,
                        )
                    )
                    if len(claims) > len(tokens):
                        raise RuntimeError(
                            "Inference repository returned more claims than requested"
                        )
                except Exception:
                    self._return_capacity(capacity, tokens)
                    logger.exception("Inference producer failed to claim activities")
                    await polling.wait(stop_event)
                    continue
                else:
                    for claim in claims:
                        await work_queue.put(_ClaimWork(claim))
                    self._return_capacity(capacity, tokens[len(claims) :])
                    if claims:
                        polling.reset()
                        continue
            await polling.wait(stop_event)

    async def _recover_expired_leases(self, now: datetime) -> None:
        """@brief 低频回收到期 inference leases / Recover expired inference leases at a low cadence.

        @param now 当前 UTC 时刻 / Current UTC instant.
        @return None；恢复查询失败不会阻断正常 claim / None; a failed recovery query does not block normal claims.
        """

        try:
            recovered = await self._repository.recover_expired_inference_leases(now=now)
            if not recovered:
                return
            self._telemetry.counter(
                MetricName.LEASE_RECOVERIES,
                float(recovered),
                attributes={"pipeline.stage": "inference"},
            )
            logger.warning(
                "Recovered expired inference leases: count=%s",
                recovered,
                extra={
                    "event_name": EventName.INFERENCE_LEASE_RECOVERED,
                    "telemetry_attributes": {"pipeline.stage": "inference"},
                },
            )
        except Exception:
            logger.exception("Inference lease recovery failed; claim polling continues")

    async def _consume(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
    ) -> None:
        """@brief 消费 claims 并归还容量 / Consume claims and return capacity.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity 容量令牌队列 / Capacity-token queue.
        @return None / None.
        """

        while True:
            work = await work_queue.get()
            try:
                if isinstance(work, _StopConsumer):
                    return
                try:
                    await self.process_claim(work.claim)
                except StaleClaimError:
                    logger.info(
                        "Inference claim was superseded before finalization: "
                        "activity_id=%s",
                        work.claim.activity.activity_id,
                        extra={
                            "event_name": "inference.claim.superseded",
                            "telemetry_attributes": {"pipeline.stage": "inference"},
                        },
                    )
                except Exception:
                    logger.exception(
                        "Inference claim could not be finalized: activity_id=%s",
                        work.claim.activity.activity_id,
                    )
                finally:
                    capacity.put_nowait(None)
            finally:
                work_queue.task_done()

    async def _finalize_failure(
        self,
        claim: InferenceActivityClaim,
        error: Exception,
    ) -> None:
        """@brief 按错误分类与预算终结失败 / Finalize a failure by taxonomy and budget.

        @param claim 失败 claim / Failed claim.
        @param error 推理异常 / Inference exception.
        @return None / None.
        """

        failed_at = self._clock.now()
        decision = self._retry_policy.decide(
            attempt_count=claim.activity.attempt_count,
            failed_at=failed_at,
            error=error,
        )
        error_text = self._error_text(error)
        if isinstance(decision, RetryInferenceAt):
            await self._repository.retry_inference_activity(
                claim,
                failed_at=failed_at,
                retry_at=decision.at,
                error=error_text,
            )
            self._telemetry.counter(
                MetricName.INFERENCE_OUTCOMES,
                attributes={"outcome": Outcome.RETRY},
            )
            return
        await self._repository.fail_inference_activity(
            claim,
            failed_at=failed_at,
            error=error_text,
        )
        self._telemetry.counter(
            MetricName.INFERENCE_OUTCOMES,
            attributes={"outcome": Outcome.DROPPED},
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        """@brief 构造有界可观测错误文本 / Build bounded observable error text.

        @param error 推理异常 / Inference exception.
        @return 最多 2000 字符摘要 / Error summary of at most 2,000 characters.
        """

        detail = str(error).strip() or error.__class__.__name__
        if not isinstance(error, InferenceError):
            return f"{error.__class__.__name__}: {detail}"[:2000]
        attributes = [f"category={error.category.value}"]
        if isinstance(error, RetryableInferenceError) and error.retry_after is not None:
            attributes.append(
                f"retry_after_seconds={error.retry_after.total_seconds():g}"
            )
        return (f"{error.__class__.__name__}[{','.join(attributes)}]: {detail}")[:2000]

    @staticmethod
    def _take_available(capacity: asyncio.Queue[None]) -> list[None]:
        """@brief 非阻塞取出全部空闲容量 / Non-blockingly take all free capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @return 本轮可用令牌 / Available tokens for this poll.
        """

        tokens: list[None] = []
        while True:
            try:
                tokens.append(capacity.get_nowait())
            except asyncio.QueueEmpty:
                return tokens

    @staticmethod
    def _return_capacity(
        capacity: asyncio.Queue[None],
        tokens: Sequence[None],
    ) -> None:
        """@brief 归还未使用容量 / Return unused capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @param tokens 待归还令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity.put_nowait(token)


__all__ = [
    "FailInferenceFinal",
    "FullJitterInferenceRetryPolicy",
    "InferenceAttemptTimeout",
    "InferenceError",
    "InferenceErrorCategory",
    "InferenceDependencyPending",
    "InferenceFailureDecision",
    "InferenceOutboundIntent",
    "InferenceOutputError",
    "InferencePersistence",
    "InferencePort",
    "InferenceResult",
    "InferenceRuntimeLimits",
    "InferenceWorker",
    "PermanentInferenceError",
    "RetryInferenceAt",
    "RetryableInferenceError",
]
