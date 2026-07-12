"""@brief Durable inbox 的有界领取与分派 worker / Bounded claim-and-dispatch worker for the durable inbox."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.application.runtime import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.domain.conversation.inbox import (
    InboundClaim,
    InboundUpdate,
)

from .router import (
    AmbiguousPrimaryRouteError,
    DispatchDeferred,
    RouteOutcome,
)


logger = logging.getLogger(__name__)


class InboxPersistence(Protocol):
    """@brief inbox worker 所需的最小持久化端口 / Minimal persistence port required by the inbox worker."""

    async def claim_inbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[InboundClaim]:
        """@brief 原子领取到期 Update / Atomically claim due Updates.

        @param now 当前 UTC 时刻 / Current UTC time.
        @param limit 最大领取数 / Maximum claim count.
        @param lease_for claim 租约 / Claim lease duration.
        @return 带 fencing token 的 claims / Claims carrying fencing tokens.
        """

        ...

    async def mark_inbound_processed(
        self,
        claim: InboundClaim,
        *,
        processed_at: datetime,
    ) -> None:
        """@brief 以 fencing token 完成 Update / Complete an Update with its fencing token.

        @param claim 当前 claim / Current claim.
        @param processed_at 完成时间 / Completion time.
        @return None / None.
        """

        ...

    async def retry_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 安排重试 / Schedule retry with the fencing token.

        @param claim 当前 claim / Current claim.
        @param failed_at 本次失败时间 / Time of this failure.
        @param retry_at 下次领取时间 / Next claim time.
        @param error 规范错误摘要 / Normalized error summary.
        @return None / None.
        """

        ...

    async def fail_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 隔离永久失败 Update / Quarantine a permanently failed Update with the fencing token.

        @param claim 当前 claim / Current claim.
        @param failed_at 最终失败时间 / Final failure time.
        @param error 规范错误摘要 / Normalized error summary.
        @return None / None.
        """

        ...

    async def recover_expired_inbound_leases(self, *, now: datetime) -> int:
        """@brief 回收到期 inbox leases / Recover expired inbox leases.

        @param now 当前 UTC 时刻 / Current UTC time.
        @return 回收数量 / Number recovered.
        """

        ...


class InboxRoute(Protocol):
    """@brief durable Update router 的窄端口 / Narrow port for a durable-Update router."""

    async def route(self, update: InboundUpdate) -> RouteOutcome:
        """@brief 路由一个已领取 Update / Route one claimed Update.

        @param update 已领取 Update / Claimed Update.
        @return route 结果 / Route outcome.
        """

        ...


class PermanentIngressError(RuntimeError):
    """@brief 不应自动重试的入口错误 / Ingress error that must not be retried automatically."""


@dataclass(frozen=True, slots=True)
class RetryAt:
    """@brief 在指定时刻重试 / Retry at a specified time.

    @param at 下一次可领取时刻 / Next claimable time.
    """

    at: datetime


@dataclass(frozen=True, slots=True)
class FailFinal:
    """@brief 将 Update 移入最终失败隔离区 / Move the Update to final-failure quarantine."""


type FailureDecision = RetryAt | FailFinal
"""@brief inbox 错误的穷尽策略决定 / Exhaustive policy decision for an inbox failure."""


@dataclass(frozen=True, slots=True)
class FullJitterRetryPolicy:
    """@brief 有上限的指数退避与 full jitter 策略 / Capped exponential-backoff policy with full jitter.

    @param max_attempts 包含首次 claim 的最大尝试数 / Maximum attempts including the first claim.
    @param initial_delay 第一次重试的最大延迟 / Maximum delay for the first retry.
    @param max_delay 所有重试的最大延迟 / Maximum delay for all retries.
    @param jitter 可注入随机源 / Injectable random source.
    """

    max_attempts: int = 8
    initial_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    jitter: Jitter = random.uniform

    def __post_init__(self) -> None:
        """@brief 校验退避参数 / Validate backoff parameters.

        @return None / None.
        @raise ValueError 次数或延迟非法时抛出 / Raised for invalid attempt or delay bounds.
        """

        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay <= timedelta():
            raise ValueError("initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay cannot be smaller than initial_delay")

    def decide(
        self,
        *,
        attempt_count: int,
        failed_at: datetime,
        error: Exception,
    ) -> FailureDecision:
        """@brief 计算永久失败或下一次重试时间 / Decide final failure or the next retry time.

        @param attempt_count repository 已记录的 claim 次数 / Claim count recorded by the repository.
        @param failed_at 本次失败时间 / Time of this failure.
        @param error 失败异常 / Failure exception.
        @return retry 或 final 决定 / Retry or final decision.
        """

        if isinstance(
            error,
            PermanentIngressError | AmbiguousPrimaryRouteError | ValueError | TypeError,
        ):
            return FailFinal()
        if attempt_count >= self.max_attempts:
            return FailFinal()
        exponent = max(0, attempt_count - 1)
        cap_seconds = min(
            self.max_delay.total_seconds(),
            self.initial_delay.total_seconds() * (2**exponent),
        )
        delay_seconds = self.jitter(0.0, cap_seconds)
        return RetryAt(failed_at + timedelta(seconds=delay_seconds))


class RuntimeAdmissionError(RuntimeError):
    """@brief runtime 暂时拒绝 inbox 操作 / Runtime temporarily rejected an inbox operation."""

    def __init__(self, deferred: DispatchDeferred) -> None:
        """@brief 从类型化 deferred 结果创建异常 / Create an error from a typed deferred outcome.

        @param deferred router 返回的准入失败 / Admission failure returned by the router.
        """

        self.deferred = deferred
        super().__init__(
            f"runtime deferred {deferred.operation_name}: {deferred.cause}"
        )


@dataclass(frozen=True, slots=True)
class _ClaimWork:
    """@brief worker queue 中的 claim / Claim carried by the worker queue.

    @param claim 待路由 claim / Claim to route.
    """

    claim: InboundClaim


@dataclass(frozen=True, slots=True)
class _StopConsumer:
    """@brief consumer drain 完成哨兵 / Sentinel indicating consumer drain completion."""


type _WorkItem = _ClaimWork | _StopConsumer
"""@brief inbox consumer 工作项 / Inbox-consumer work item."""


class InboxWorker:
    """@brief 有界领取、路由并终结 durable Updates / Bounded worker that claims, routes, and finalizes durable Updates."""

    def __init__(
        self,
        *,
        repository: InboxPersistence,
        router: InboxRoute,
        worker_count: int,
        poll_interval: float,
        lease_for: timedelta,
        retry_policy: FullJitterRetryPolicy | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 创建 inbox worker / Create an inbox worker.

        @param repository inbox 持久化端口 / Inbox persistence port.
        @param router 显式 route pipeline / Explicit route pipeline.
        @param worker_count 已领取但未终结 Update 的上限 / Maximum claimed-but-unfinalized Updates.
        @param poll_interval 空闲轮询秒数 / Idle polling interval in seconds.
        @param lease_for 每个 claim 的租约时长 / Lease duration per claim.
        @param retry_policy 错误退避策略 / Failure-backoff policy.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        """

        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if lease_for <= timedelta():
            raise ValueError("lease_for must be positive")
        self._repository = repository
        self._router = router
        self._worker_count = worker_count
        self._poll_interval = poll_interval
        self._lease_for = lease_for
        self._retry_policy = retry_policy or FullJitterRetryPolicy()
        self._clock = clock or SystemUtcClock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行 producer 与固定数量 consumers / Run one producer and a fixed number of consumers.

        @param stop_event 置位后停止领取并 drain 已领取 Update / Stops claims and drains claimed Updates when set.
        @return None / None.
        @note ``TaskGroup`` 拥有全部 consumer；没有 detached Task。/
        A ``TaskGroup`` owns every consumer; no task is detached.
        """

        work_queue: asyncio.Queue[_WorkItem] = asyncio.Queue(maxsize=self._worker_count)
        capacity: asyncio.Queue[None] = asyncio.Queue(maxsize=self._worker_count)
        for _ in range(self._worker_count):
            capacity.put_nowait(None)

        async with asyncio.TaskGroup() as task_group:
            for index in range(self._worker_count):
                task_group.create_task(
                    self._consume(work_queue, capacity),
                    name=f"inbox-consumer-{index}",
                )
            try:
                await self._produce(work_queue, capacity, stop_event)
            except BaseException:
                stop_event.set()
                raise
            else:
                stop_event.set()
                await work_queue.join()
                for _ in range(self._worker_count):
                    await work_queue.put(_StopConsumer())

    async def process_claim(self, claim: InboundClaim) -> None:
        """@brief 路由一个 claim 并以 fencing token 终结状态 / Route one claim and finalize it with the fencing token.

        @param claim 当前 claim / Current claim.
        @return None / None.
        @note ``CancelledError`` 不会被捕获；取消留下 processing claim，由 lease recovery 重建。/
        ``CancelledError`` is not caught; cancellation leaves a processing claim for lease recovery.
        """

        try:
            outcome = await self._router.route(claim.update)
            if isinstance(outcome, DispatchDeferred):
                raise RuntimeAdmissionError(outcome)
        except Exception as error:
            await self._finalize_failure(claim, error)
            return
        await self._repository.mark_inbound_processed(
            claim,
            processed_at=self._clock.now(),
        )

    async def _produce(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 在容量允许时回收 leases 并领取 Updates / Recover leases and claim Updates when capacity permits.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param capacity 未占用 claim 容量令牌 / Free claim-capacity tokens.
        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            tokens = self._take_available(capacity)
            if tokens:
                now = self._clock.now()
                try:
                    recovered = await self._repository.recover_expired_inbound_leases(
                        now=now
                    )
                    if recovered:
                        logger.warning(
                            "Recovered expired inbox leases: count=%s", recovered
                        )
                    claims = tuple(
                        await self._repository.claim_inbound(
                            now=now,
                            limit=len(tokens),
                            lease_for=self._lease_for,
                        )
                    )
                    if len(claims) > len(tokens):
                        raise RuntimeError(
                            "Inbox repository returned more claims than requested"
                        )
                except Exception:
                    self._return_capacity(capacity, tokens)
                    logger.exception(
                        "Inbox producer failed to recover or claim Updates"
                    )
                else:
                    for claim in claims:
                        await work_queue.put(_ClaimWork(claim))
                    self._return_capacity(capacity, tokens[len(claims) :])
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def _consume(
        self,
        work_queue: asyncio.Queue[_WorkItem],
        capacity: asyncio.Queue[None],
    ) -> None:
        """@brief 消费 claims 并归还容量 / Consume claims and return capacity.

        @param work_queue 有界工作队列 / Bounded work queue.
        @param capacity claim 容量令牌 / Claim-capacity tokens.
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
                        "Inbox claim could not be finalized: update_id=%s",
                        work.claim.update.update_id.value,
                    )
                finally:
                    capacity.put_nowait(None)
            finally:
                work_queue.task_done()

    async def _finalize_failure(self, claim: InboundClaim, error: Exception) -> None:
        """@brief 按策略安排重试或隔离 / Schedule retry or quarantine according to policy.

        @param claim 失败 claim / Failed claim.
        @param error route 异常 / Route exception.
        @return None / None.
        """

        failed_at = self._clock.now()
        decision = self._retry_policy.decide(
            attempt_count=claim.update.attempt_count,
            failed_at=failed_at,
            error=error,
        )
        error_text = self._error_text(error)
        if isinstance(decision, RetryAt):
            await self._repository.retry_inbound(
                claim,
                failed_at=failed_at,
                retry_at=decision.at,
                error=error_text,
            )
            return
        await self._repository.fail_inbound(
            claim,
            failed_at=failed_at,
            error=error_text,
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        """@brief 规范并限制持久化错误文本 / Normalize and bound persisted error text.

        @param error 失败异常 / Failure exception.
        @return 最多 2000 字符的摘要 / Summary of at most 2,000 characters.
        """

        detail = str(error).strip() or error.__class__.__name__
        return f"{error.__class__.__name__}: {detail}"[:2000]

    @staticmethod
    def _take_available(capacity: asyncio.Queue[None]) -> list[None]:
        """@brief 非阻塞取出全部空闲容量 / Non-blockingly take all free capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @return 本轮可用令牌 / Tokens available this round.
        """

        tokens: list[None] = []
        while True:
            try:
                tokens.append(capacity.get_nowait())
            except asyncio.QueueEmpty:
                return tokens

    @staticmethod
    def _return_capacity(capacity: asyncio.Queue[None], tokens: Sequence[None]) -> None:
        """@brief 归还未使用 claim 容量 / Return unused claim capacity.

        @param capacity 容量令牌队列 / Capacity-token queue.
        @param tokens 待归还令牌 / Tokens to return.
        @return None / None.
        """

        for token in tokens:
            capacity.put_nowait(token)


__all__ = [
    "FailFinal",
    "FailureDecision",
    "FullJitterRetryPolicy",
    "InboxPersistence",
    "InboxRoute",
    "InboxWorker",
    "PermanentIngressError",
    "RetryAt",
    "RuntimeAdmissionError",
]
