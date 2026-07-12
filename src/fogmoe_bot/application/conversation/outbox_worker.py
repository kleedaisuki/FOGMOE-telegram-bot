"""@brief Transactional outbox 的有界投递 worker / Bounded transactional-outbox delivery worker.

Worker 只依赖会话领域值对象和用例局部窄端口。Repository 已在数据库领取时保证
delivery-stream head 顺序，因此这里不建立进程锁。Telegram 等外部系统通常不提供
通用幂等键；本实现提供 at-least-once 投递，模糊超时后的重试可能产生重复消息。
/ The worker depends only on conversation-domain values and use-case-local narrow
ports. Repository claims already enforce delivery-stream-head ordering, so no
process lock is added here. External systems such as Telegram expose no general
idempotency key; delivery is at-least-once and retrying ambiguous timeouts can
produce duplicate messages.
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

from fogmoe_bot.application.runtime import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.outbox import (
    OutboundClaim,
    OutboundMessage,
)
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus


logger = logging.getLogger(__name__)


class OutboxPersistence(Protocol):
    """@brief outbox worker 所需的最小持久化端口 / Minimal persistence port for the outbox worker."""

    async def claim_outbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[OutboundClaim]:
        """@brief 原子领取可投递消息 / Atomically claim deliverable messages.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum claim count.
        @param lease_for fencing 租约时长 / Fencing lease duration.
        @return 带 fencing token 的 claims / Claims carrying fencing tokens.
        """

        ...

    async def mark_outbound_delivered(
        self,
        claim: OutboundClaim,
        *,
        delivered_at: datetime,
        external_message_id: str | None,
    ) -> None:
        """@brief 以 fencing token 确认投递 / Acknowledge delivery with the fencing token.

        @param claim 当前 claim / Current claim.
        @param delivered_at 成功时间 / Delivery time.
        @param external_message_id 外部消息标识 / External message identifier.
        @return None / None.
        """

        ...

    async def retry_outbound(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 安排重试 / Schedule a retry with the fencing token.

        @param claim 当前 claim / Current claim.
        @param failed_at 本次失败时间 / Failure time.
        @param retry_at 下次可领取时间 / Next claimable time.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        """

        ...

    async def fail_outbound(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 标记永久失败 / Mark final failure with the fencing token.

        @param claim 当前 claim / Current claim.
        @param failed_at 最终失败时间 / Final-failure time.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        """

        ...

    async def recover_expired_outbound_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃或取消留下的过期租约 / Recover expired leases left by crashes or cancellation.

        @param now 当前 UTC 时间 / Current UTC time.
        @return 回收数量 / Number recovered.
        """

        ...


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """@brief 外部投递回执 / External-delivery receipt.

    @param external_message_id 外部消息标识 / External message identifier.
    """

    external_message_id: str | None


class OutboundDelivery(Protocol):
    """@brief 单条出站消息投递端口 / Port delivering one outbound message."""

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        """@brief 执行一次外部投递尝试 / Perform one external-delivery attempt.

        @param message 已领取 outbox 消息 / Claimed outbox message.
        @return 外部投递回执 / External-delivery receipt.
        @note 实现不得吞掉 CancelledError / Implementations must not swallow CancelledError.
        """

        ...


class DeliveryErrorCategory(StrEnum):
    """@brief 可持久化的投递错误分类 / Persistable delivery-error category."""

    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    AMBIGUOUS_TIMEOUT = "ambiguous_timeout"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    INVALID_PAYLOAD = "invalid_payload"
    UNSUPPORTED_KIND = "unsupported_kind"
    PROVIDER = "provider"


class DeliveryError(RuntimeError):
    """@brief 已分类的外部投递错误 / Classified external-delivery error.

    @param message 错误详情 / Error detail.
    @param category 稳定错误分类 / Stable error category.
    @param outcome_ambiguous 请求可能已经成功 / The request may already have succeeded.
    """

    category: DeliveryErrorCategory
    outcome_ambiguous: bool

    def __init__(
        self,
        message: str,
        *,
        category: DeliveryErrorCategory,
        outcome_ambiguous: bool = False,
    ) -> None:
        """@brief 创建投递错误 / Create a delivery error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        @param outcome_ambiguous 请求结果是否未知 / Whether the request outcome is unknown.
        """

        super().__init__(message)
        self.category = category
        self.outcome_ambiguous = outcome_ambiguous


class RetryableDeliveryError(DeliveryError):
    """@brief 可以在预算内重试的投递错误 / Delivery error retryable within budget.

    @param retry_after Provider 指定的最小等待时间 / Provider-specified minimum delay.
    """

    retry_after: timedelta | None

    def __init__(
        self,
        message: str,
        *,
        category: DeliveryErrorCategory,
        retry_after: timedelta | None = None,
        outcome_ambiguous: bool = False,
    ) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 错误详情 / Error detail.
        @param category 稳定错误分类 / Stable error category.
        @param retry_after Provider 最小等待时间 / Provider minimum delay.
        @param outcome_ambiguous 请求结果是否未知 / Whether the request outcome is unknown.
        @raise ValueError retry_after 非正时抛出 / Raised for a non-positive retry_after.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("retry_after must be positive")
        super().__init__(
            message,
            category=category,
            outcome_ambiguous=outcome_ambiguous,
        )
        self.retry_after = retry_after


class AmbiguousDeliveryTimeout(RetryableDeliveryError):
    """@brief 结果未知的投递超时 / Delivery timeout with an unknown outcome."""

    def __init__(self, message: str) -> None:
        """@brief 创建模糊超时 / Create an ambiguous timeout.

        @param message 超时详情 / Timeout detail.
        """

        super().__init__(
            message,
            category=DeliveryErrorCategory.AMBIGUOUS_TIMEOUT,
            outcome_ambiguous=True,
        )


class PermanentDeliveryError(DeliveryError):
    """@brief 不应自动重试的投递错误 / Delivery error that must not be retried automatically."""


class OutboundPayloadError(PermanentDeliveryError):
    """@brief 出站 kind 或 payload 非法 / Invalid outbound kind or payload."""

    def __init__(
        self,
        message: str,
        *,
        category: DeliveryErrorCategory = DeliveryErrorCategory.INVALID_PAYLOAD,
    ) -> None:
        """@brief 创建 payload 错误 / Create a payload error.

        @param message 错误详情 / Error detail.
        @param category payload 或 kind 分类 / Payload or kind category.
        """

        super().__init__(message, category=category)


@dataclass(frozen=True, slots=True)
class RetryDeliveryAt:
    """@brief 在指定时刻重试投递 / Retry delivery at a specified time.

    @param at 下次可领取时间 / Next claimable time.
    """

    at: datetime


@dataclass(frozen=True, slots=True)
class FailDeliveryFinal:
    """@brief 将 outbox 消息标记永久失败 / Mark the outbox message finally failed."""


type DeliveryFailureDecision = RetryDeliveryAt | FailDeliveryFinal
"""@brief 投递失败的穷尽策略决定 / Exhaustive delivery-failure decision."""


@dataclass(frozen=True, slots=True)
class FullJitterDeliveryRetryPolicy:
    """@brief 指数退避、Full Jitter 与 Retry-After 策略 / Exponential-backoff, Full-Jitter, and Retry-After policy.

    @param max_attempts 包含首次 claim 的最大尝试数 / Maximum attempts including the first claim.
    @param initial_delay 第一次重试的指数上限 / Exponential cap for the first retry.
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
    ) -> DeliveryFailureDecision:
        """@brief 决定重试时刻或永久失败 / Decide a retry time or final failure.

        @param attempt_count Repository 已记录的领取次数 / Claim count recorded by the repository.
        @param failed_at 本次失败时间 / Failure time.
        @param error 投递异常 / Delivery exception.
        @return 重试或永久失败决定 / Retry or final-failure decision.
        """

        failure_time = ensure_utc(failed_at)
        if isinstance(error, PermanentDeliveryError | ValueError | TypeError):
            return FailDeliveryFinal()
        if attempt_count >= self.max_attempts:
            return FailDeliveryFinal()

        if isinstance(error, RetryableDeliveryError) and error.retry_after is not None:
            provider_seconds = error.retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            extra_seconds = self._sample(0.0, jitter_cap)
            return RetryDeliveryAt(
                failure_time + error.retry_after + timedelta(seconds=extra_seconds)
            )

        exponent = max(0, attempt_count - 1)
        cap_seconds = min(
            self.max_delay.total_seconds(),
            self.initial_delay.total_seconds() * (2**exponent),
        )
        sampled_delay = timedelta(seconds=self._sample(0.0, cap_seconds))
        return RetryDeliveryAt(
            failure_time
            + (sampled_delay if sampled_delay > timedelta() else timedelta.resolution)
        )

    def _sample(self, lower: float, upper: float) -> float:
        """@brief 验证并返回 jitter 样本 / Validate and return a jitter sample.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 合法抖动秒数 / Valid jitter seconds.
        @raise ValueError 随机源越界或返回非有限值时抛出 / Raised for an out-of-range or non-finite sample.
        """

        value = self.jitter(lower, upper)
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("jitter returned a value outside its requested interval")
        return value


@dataclass(frozen=True, slots=True)
class _ClaimWork:
    """@brief consumer queue 中的 claim / Claim in the consumer queue.

    @param claim 待投递 claim / Claim to deliver.
    """

    claim: OutboundClaim


@dataclass(frozen=True, slots=True)
class _StopConsumer:
    """@brief consumer 正常 drain 后的停止哨兵 / Stop sentinel after normal consumer drain."""


type _WorkItem = _ClaimWork | _StopConsumer
"""@brief outbox consumer 工作项 / Outbox-consumer work item."""


class OutboxWorker:
    """@brief 有界领取、投递并终结 outbox 消息 / Bounded worker claiming, delivering, and finalizing outbox messages."""

    def __init__(
        self,
        *,
        repository: OutboxPersistence,
        delivery: OutboundDelivery,
        worker_count: int,
        poll_interval: float,
        lease_for: timedelta,
        attempt_timeout: timedelta,
        retry_policy: FullJitterDeliveryRetryPolicy | None = None,
        clock: UtcClock | None = None,
        telemetry: Telemetry,
    ) -> None:
        """@brief 创建 outbox worker / Create an outbox worker.

        @param repository outbox 持久化端口 / Outbox persistence port.
        @param delivery 外部投递端口 / External-delivery port.
        @param worker_count 已领取未终结消息上限 / Maximum claimed-but-unfinalized messages.
        @param poll_interval 空闲轮询秒数 / Idle polling interval in seconds.
        @param lease_for 每次 claim 的租约 / Lease duration for each claim.
        @param attempt_timeout 单次外部调用上限 / External-call timeout per attempt.
        @param retry_policy 失败策略 / Failure policy.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @return None / None.
        @raise ValueError 容量或时间参数非法时抛出 / Raised for invalid capacity or timing parameters.
        """

        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if lease_for <= timedelta():
            raise ValueError("lease_for must be positive")
        if attempt_timeout <= timedelta():
            raise ValueError("attempt_timeout must be positive")
        if attempt_timeout >= lease_for:
            raise ValueError("attempt_timeout must be shorter than lease_for")
        self._repository = repository
        self._delivery = delivery
        self._worker_count = worker_count
        self._poll_interval = poll_interval
        self._lease_for = lease_for
        self._attempt_timeout = attempt_timeout
        self._retry_policy = retry_policy or FullJitterDeliveryRetryPolicy()
        self._clock = clock or SystemUtcClock()
        self._telemetry = telemetry

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行 producer 与固定 consumers / Run one producer and fixed consumers.

        @param stop_event 置位后停止领取并 drain / Stops claiming and drains when set.
        @return None / None.
        @note 正常 shutdown 会 drain；Task 取消会立即传播，processing claim 由 lease recovery 回收。/
        Normal shutdown drains; task cancellation propagates immediately and lease recovery reclaims processing messages.
        """

        work_queue: asyncio.Queue[_WorkItem] = asyncio.Queue(maxsize=self._worker_count)
        capacity: asyncio.Queue[None] = asyncio.Queue(maxsize=self._worker_count)
        for _ in range(self._worker_count):
            capacity.put_nowait(None)

        async with asyncio.TaskGroup() as task_group:
            for index in range(self._worker_count):
                task_group.create_task(
                    self._consume(work_queue, capacity),
                    name=f"outbox-consumer-{index}",
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

    async def process_claim(self, claim: OutboundClaim) -> None:
        """@brief 投递并以 fencing token 终结 claim / Deliver and finalize a claim with its fencing token.

        @param claim 当前 claim / Current claim.
        @return None / None.
        @note CancelledError 不被捕获；claim 保持 processing，等待租约回收。/
        CancelledError is not caught; the claim remains processing until lease recovery.
        """

        message = claim.message
        with self._telemetry.span(
            "outbox.deliver",
            kind=SpanKind.PRODUCER,
            parent=message.draft.trace_context,
            attributes={
                "fogmoe.outbound.id": str(message.message_id),
                "fogmoe.turn.id": str(message.turn_id) if message.turn_id else "",
                "fogmoe.outbound.kind": message.kind.value,
                "fogmoe.outbox.attempt": message.attempt_count,
            },
        ) as span:
            try:
                async with asyncio.timeout(self._attempt_timeout.total_seconds()):
                    receipt = await self._delivery.deliver(message)
            except TimeoutError:
                error = AmbiguousDeliveryTimeout(
                    f"delivery attempt exceeded {self._attempt_timeout.total_seconds():g}s"
                )
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                await self._finalize_failure(claim, error)
                return
            except Exception as error:
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", error.__class__.__name__)
                await self._finalize_failure(claim, error)
                return

            await self._repository.mark_outbound_delivered(
                claim,
                delivered_at=self._clock.now(),
                external_message_id=receipt.external_message_id,
            )

    async def _produce(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 按可用容量回收租约并领取消息 / Recover leases and claim messages up to capacity.

        @param work_queue 有界 claim 队列 / Bounded claim queue.
        @param capacity 空闲容量令牌 / Free-capacity tokens.
        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            tokens = self._take_available(capacity)
            if tokens:
                now = self._clock.now()
                try:
                    recovered = await self._repository.recover_expired_outbound_leases(
                        now=now
                    )
                    if recovered:
                        self._telemetry.counter(
                            "fogmoe.outbox.leases.recovered",
                            float(recovered),
                        )
                        logger.warning(
                            "Recovered expired outbox leases: count=%s",
                            recovered,
                        )
                    claims = tuple(
                        await self._repository.claim_outbound(
                            now=now,
                            limit=len(tokens),
                            lease_for=self._lease_for,
                        )
                    )
                    if len(claims) > len(tokens):
                        raise RuntimeError(
                            "Outbox repository returned more claims than requested"
                        )
                except Exception:
                    self._return_capacity(capacity, tokens)
                    logger.exception(
                        "Outbox producer failed to recover or claim messages"
                    )
                else:
                    for claim in claims:
                        await work_queue.put(_ClaimWork(claim))
                    self._return_capacity(capacity, tokens[len(claims) :])
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except TimeoutError:
                continue

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
                except Exception:
                    logger.exception(
                        "Outbox claim could not be finalized: message_id=%s",
                        work.claim.message.message_id,
                    )
                finally:
                    capacity.put_nowait(None)
            finally:
                work_queue.task_done()

    async def _finalize_failure(
        self,
        claim: OutboundClaim,
        error: Exception,
    ) -> None:
        """@brief 按错误分类和预算终结失败 / Finalize a failure by taxonomy and budget.

        @param claim 失败 claim / Failed claim.
        @param error 投递异常 / Delivery exception.
        @return None / None.
        """

        failed_at = self._clock.now()
        decision = self._retry_policy.decide(
            attempt_count=claim.message.attempt_count,
            failed_at=failed_at,
            error=error,
        )
        error_text = self._error_text(error)
        if isinstance(decision, RetryDeliveryAt):
            await self._repository.retry_outbound(
                claim,
                failed_at=failed_at,
                retry_at=decision.at,
                error=error_text,
            )
            return
        await self._repository.fail_outbound(
            claim,
            failed_at=failed_at,
            error=error_text,
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        """@brief 构造有界且可观测的错误文本 / Build bounded observable error text.

        @param error 投递异常 / Delivery exception.
        @return 最多 2000 字符的错误摘要 / Error summary of at most 2,000 characters.
        """

        detail = str(error).strip() or error.__class__.__name__
        if not isinstance(error, DeliveryError):
            return f"{error.__class__.__name__}: {detail}"[:2000]
        attributes = [
            f"category={error.category.value}",
            f"outcome_ambiguous={str(error.outcome_ambiguous).lower()}",
        ]
        if isinstance(error, RetryableDeliveryError) and error.retry_after is not None:
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
    "AmbiguousDeliveryTimeout",
    "DeliveryError",
    "DeliveryErrorCategory",
    "DeliveryFailureDecision",
    "DeliveryReceipt",
    "FailDeliveryFinal",
    "FullJitterDeliveryRetryPolicy",
    "OutboxPersistence",
    "OutboxWorker",
    "OutboundDelivery",
    "OutboundPayloadError",
    "PermanentDeliveryError",
    "RetryDeliveryAt",
    "RetryableDeliveryError",
]
