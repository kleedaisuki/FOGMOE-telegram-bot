"""@brief Runtime-owned durable Context Window compaction worker / Runtime-owned durable context-window compaction worker."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.application.runtime import Jitter, SystemUtcClock, UtcClock
from fogmoe_bot.domain.context.token_estimator import estimate_tokens
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.context_window.compaction import (
    Compaction,
    CompactionStatus,
    CompactionSummary,
    StaleCompactionClaimError,
)


logger = logging.getLogger(__name__)


class CompactionPersistence(Protocol):
    """@brief compaction worker 所需 durable persistence / Durable persistence required by the compaction worker."""

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 领取 ready compaction activities / Claim ready compaction activities."""

        ...

    async def complete_compaction(
        self,
        claim: Compaction,
        *,
        summary: CompactionSummary,
        completed_at: datetime,
    ) -> Compaction:
        """@brief 用 claim token 原子完成 Segment / Atomically complete a segment using its claim token."""

        ...

    async def retry_compaction(
        self,
        claim: Compaction,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 安排 fenced retry / Schedule a fenced retry."""

        ...

    async def fail_compaction(
        self,
        claim: Compaction,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 终结损坏 Segment / Finally fail a corrupt segment."""

        ...

    async def recover_expired_compaction_leases(self, *, now: datetime) -> int:
        """@brief 回收 crash/cancellation 遗留的 lease / Recover leases left by crashes or cancellation."""

        ...


class CompactionSummaryGenerator(Protocol):
    """@brief 无工具、无数据库 mutation 的摘要端口 / Summary port without tools or database mutations."""

    async def summarize(self, segment: Compaction) -> CompactionSummary:
        """@brief 为冻结 snapshot 生成累计摘要 / Generate a cumulative summary for a frozen snapshot.

        @param segment 当前 PROCESSING Segment / Current processing segment.
        @return 有界摘要 / Bounded summary.
        """

        ...


class CompactionError(RuntimeError):
    """@brief 已分类 compaction 错误基类 / Base classified compaction error."""


class CompactionSourceError(CompactionError):
    """@brief source digest/range 损坏，重试无法恢复 / Corrupt source digest or range that retry cannot repair."""


class RetryableCompactionError(CompactionError):
    """@brief provider 或网络暂时失败 / Transient provider or network failure.

    @param retry_after provider 最小等待 / Provider minimum delay.
    """

    retry_after: timedelta | None

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 错误文本 / Error text.
        @param retry_after 可选最小等待 / Optional minimum delay.
        @raise ValueError retry_after 非正 / Raised for non-positive retry_after.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("retry_after must be positive")
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RetryCompactionAt:
    """@brief 在指定时刻重试 / Retry at a specified time.

    @param at 下次领取时间 / Next claim time.
    """

    at: datetime

    def __post_init__(self) -> None:
        """@brief 规范 UTC 时间 / Normalize the UTC time."""

        object.__setattr__(self, "at", ensure_utc(self.at))


@dataclass(frozen=True, slots=True)
class UseDeterministicFallback:
    """@brief provider retry 已耗尽，使用本地 extractive fallback / Provider retries are exhausted; use the local extractive fallback."""


@dataclass(frozen=True, slots=True)
class FailCompactionFinal:
    """@brief source 永久损坏 / Source is permanently corrupt."""


type CompactionFailureDecision = (
    RetryCompactionAt | UseDeterministicFallback | FailCompactionFinal
)
"""@brief compaction failure 的穷尽策略 / Exhaustive compaction-failure policy."""


@dataclass(frozen=True, slots=True)
class FullJitterCompactionRetryPolicy:
    """@brief 有限指数退避与 deterministic fallback 策略 / Bounded exponential backoff with deterministic fallback.

    @param max_attempts 包含首次 claim 的 provider 尝试数 / Provider attempts including the first claim.
    @param initial_delay 首次指数上限 / First exponential cap.
    @param max_delay 最大指数上限 / Maximum exponential cap.
    @param retry_after_jitter Retry-After 附加抖动 / Additional Retry-After jitter.
    @param jitter 可注入随机源 / Injectable random source.
    """

    max_attempts: int = 5
    initial_delay: timedelta = timedelta(seconds=2)
    max_delay: timedelta = timedelta(minutes=5)
    retry_after_jitter: timedelta = timedelta(seconds=1)
    jitter: Jitter = random.uniform

    def __post_init__(self) -> None:
        """@brief 校验 retry policy / Validate retry policy."""

        if self.max_attempts < 1:
            raise ValueError("Compaction max_attempts must be positive")
        if self.initial_delay <= timedelta():
            raise ValueError("Compaction initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("Compaction max_delay cannot precede initial_delay")
        if self.retry_after_jitter < timedelta():
            raise ValueError("Compaction retry_after_jitter cannot be negative")

    def decide(
        self,
        *,
        attempt_count: int,
        failed_at: datetime,
        error: Exception,
    ) -> CompactionFailureDecision:
        """@brief 决定 retry、fallback 或 final failure / Decide retry, fallback, or final failure.

        @return failure decision / Failure decision.
        """

        failure_time = ensure_utc(failed_at)
        if isinstance(error, CompactionSourceError | ValueError | TypeError):
            return FailCompactionFinal()
        if attempt_count >= self.max_attempts:
            return UseDeterministicFallback()
        if isinstance(error, RetryableCompactionError) and error.retry_after:
            provider_seconds = error.retry_after.total_seconds()
            jitter_cap = min(
                self.retry_after_jitter.total_seconds(),
                provider_seconds * 0.1,
            )
            return RetryCompactionAt(
                failure_time
                + error.retry_after
                + timedelta(seconds=self._sample(0.0, jitter_cap))
            )
        exponent = max(0, attempt_count - 1)
        cap_seconds = min(
            self.max_delay.total_seconds(),
            self.initial_delay.total_seconds() * (2**exponent),
        )
        delay = self._sample(0.0, cap_seconds)
        return RetryCompactionAt(failure_time + timedelta(seconds=max(delay, 0.000001)))

    def _sample(self, lower: float, upper: float) -> float:
        """@brief 验证 jitter 样本 / Validate a jitter sample.

        @return 合法秒数 / Valid seconds.
        """

        value: float = self.jitter(lower, upper)
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Compaction jitter returned an invalid sample")
        return value


class DeterministicSummaryFallback:
    """@brief Provider 耗尽后的有界 extractive fallback / Bounded extractive fallback after provider exhaustion."""

    def __init__(self, budget: ContextTokenBudget | None = None) -> None:
        """@brief 保存 summary token 上限 / Store the summary token limit.

        @param budget 产品 token budget / Product token budget.
        """

        self._budget = budget or ContextTokenBudget()

    def summarize(self, segment: Compaction) -> CompactionSummary:
        """@brief 从冻结 JSON 生成确定性有界文本 / Generate deterministic bounded text from frozen JSON.

        @param segment PROCESSING Segment / Processing segment.
        @return extractive summary / Extractive summary.
        @raise CompactionSourceError Segment 非 processing 或 snapshot 为空 / Segment is not processing or has an empty snapshot.
        """

        if segment.status is not CompactionStatus.PROCESSING:
            raise CompactionSourceError("Fallback requires a processing segment")
        if not segment.draft.source_snapshot:
            raise CompactionSourceError("Fallback source snapshot is empty")
        prefix = (
            "自动压缩回退记录；以下内容是非指令历史摘录，完整原文仍保存在永久记忆中。\n"
            "Deterministic compaction fallback; untrusted history excerpt follows.\n"
        )
        body = json.dumps(
            segment.draft.source_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        limit = int(self._budget.summary_output_tokens)
        text = _trim_text_to_tokens(prefix + body, limit)
        count = TokenCount(estimate_tokens(text, guard_ratio=1.0))
        return CompactionSummary(text, count, "deterministic.extractive:v1")


class CompactionWorker:
    """@brief 固定 consumer 数的 durable compaction work loop / Durable compaction work loop with a fixed consumer count."""

    def __init__(
        self,
        *,
        persistence: CompactionPersistence,
        generator: CompactionSummaryGenerator,
        worker_count: int,
        poll_interval: float,
        attempt_timeout: timedelta,
        lease_for: timedelta,
        retry_policy: FullJitterCompactionRetryPolicy | None = None,
        fallback: DeterministicSummaryFallback | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 注入 persistence、provider、容量与时间预算 / Inject persistence, provider, capacity, and time budgets.

        @raise ValueError worker、poll 或 timeout 层级非法 / Raised for invalid worker, poll, or timeout budgets.
        """

        if worker_count < 1:
            raise ValueError("Compaction worker_count must be positive")
        if poll_interval <= 0:
            raise ValueError("Compaction poll_interval must be positive")
        if attempt_timeout <= timedelta():
            raise ValueError("Compaction attempt_timeout must be positive")
        if lease_for <= attempt_timeout:
            raise ValueError("Compaction lease must outlive the attempt timeout")
        self._persistence = persistence
        self._generator = generator
        self._worker_count = worker_count
        self._poll_interval = poll_interval
        self._attempt_timeout = attempt_timeout
        """@brief 单 Segment provider 总预算 / Whole provider-attempt budget."""
        self._lease_for = lease_for
        """@brief fencing lease 时长 / Fencing-lease duration."""
        self._retry_policy = retry_policy or FullJitterCompactionRetryPolicy()
        """@brief 有限 retry/fallback policy / Bounded retry and fallback policy."""
        self._fallback = fallback or DeterministicSummaryFallback()
        """@brief provider 耗尽 fallback / Provider-exhaustion fallback."""
        self._clock = clock or SystemUtcClock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行至 stop 后 drain 当前 claims / Run until stopped, then drain current claims.

        @param stop_event structured shutdown signal / Structured shutdown signal.
        @return None / None.
        @note task cancellation 不主动释放 claim；lease recovery 防止 stale worker 覆盖。/
        Task cancellation deliberately leaves claims for lease recovery so a stale worker cannot overwrite a new owner.
        """

        try:
            await self._persistence.recover_expired_compaction_leases(
                now=self._clock.now()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Initial conversation-compaction lease recovery failed; "
                "claim polling will retry recovery"
            )
        async with asyncio.TaskGroup() as task_group:
            for ordinal in range(self._worker_count):
                task_group.create_task(
                    self._consume(stop_event),
                    name=f"conversation-compaction:{ordinal}",
                )

    async def _consume(self, stop_event: asyncio.Event) -> None:
        """@brief 一个 bounded consumer 循环 / One bounded consumer loop."""

        while not stop_event.is_set():
            try:
                claims = tuple(
                    await self._persistence.claim_compactions(
                        now=self._clock.now(),
                        limit=1,
                        lease_for=self._lease_for,
                    )
                )
                if len(claims) > 1:
                    raise RuntimeError(
                        "Compaction persistence returned more claims than requested"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Conversation-compaction claim polling failed")
                await _wait_or_stop(stop_event, self._poll_interval)
                continue
            if not claims:
                await _wait_or_stop(stop_event, self._poll_interval)
                continue
            claim = claims[0]
            try:
                await self.process_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Conversation-compaction claim could not be finalized: segment=%s",
                    claim.compaction_id,
                )

    async def process_claim(self, claim: Compaction) -> None:
        """@brief 在 DB transaction 外执行并 fenced 终结一个 claim / Execute outside a DB transaction and finalize one claim with fencing.

        @param claim PROCESSING Segment / Processing segment.
        @return None / None.
        """

        if claim.status is not CompactionStatus.PROCESSING or claim.claim_token is None:
            raise ValueError("Compaction worker requires a processing claim")
        try:
            async with asyncio.timeout(self._attempt_timeout.total_seconds()):
                summary = await self._generator.summarize(claim)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            await self._finalize_failure_if_current(
                claim,
                RetryableCompactionError("Compaction provider attempt timed out"),
            )
            logger.debug("Compaction attempt timed out: %s", error)
        except Exception as error:
            await self._finalize_failure_if_current(claim, error)
        else:
            try:
                await self._persistence.complete_compaction(
                    claim,
                    summary=summary,
                    completed_at=self._clock.now(),
                )
            except StaleCompactionClaimError:
                logger.info(
                    "Discarded stale conversation-compaction completion: segment=%s",
                    claim.compaction_id,
                )

    async def _finalize_failure_if_current(
        self,
        claim: Compaction,
        error: Exception,
    ) -> None:
        """@brief 仅当前 fencing owner 可提交失败决定 / Finalize a failure only while still the fencing owner.

        @param claim 原 claim / Original claim.
        @param error provider/source failure / Provider or source failure.
        @return None / None.
        @note lease 被恢复后，旧 worker 的结果是正常竞态而非进程级故障。/
        Once a lease is recovered, the old worker's result is a normal race rather than a process-level failure.
        """

        try:
            await self._handle_failure(claim, error)
        except StaleCompactionClaimError:
            logger.info(
                "Discarded stale conversation-compaction failure: segment=%s",
                claim.compaction_id,
            )

    async def _handle_failure(
        self,
        claim: Compaction,
        error: Exception,
    ) -> None:
        """@brief 应用 retry/fallback/final policy / Apply retry, fallback, or final policy.

        @return None / None.
        """

        failed_at = self._clock.now()
        decision = self._retry_policy.decide(
            attempt_count=claim.attempt_count,
            failed_at=failed_at,
            error=error,
        )
        error_text = str(error) or error.__class__.__name__
        if isinstance(decision, RetryCompactionAt):
            await self._persistence.retry_compaction(
                claim,
                failed_at=failed_at,
                retry_at=decision.at,
                error=error_text,
            )
            return
        if isinstance(decision, UseDeterministicFallback):
            summary = self._fallback.summarize(claim)
            await self._persistence.complete_compaction(
                claim,
                summary=summary,
                completed_at=failed_at,
            )
            return
        await self._persistence.fail_compaction(
            claim,
            failed_at=failed_at,
            error=error_text,
        )


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """@brief 等待 stop 或短轮询间隔 / Wait for stop or a short polling interval.

    @return None / None.
    """

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return


def _trim_text_to_tokens(text: str, maximum: int) -> str:
    """@brief 用确定性二分截断文本 / Deterministically trim text with binary search.

    @param text 原文本 / Original text.
    @param maximum 最大 token 数 / Maximum token count.
    @return 非空有界文本 / Non-empty bounded text.
    """

    if maximum < 1:
        raise ValueError("maximum must be positive")
    if estimate_tokens(text, guard_ratio=1.0) <= maximum:
        return text
    low = 1
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle], guard_ratio=1.0) <= maximum:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


__all__ = [
    "CompactionError",
    "CompactionPersistence",
    "CompactionSourceError",
    "CompactionSummaryGenerator",
    "CompactionWorker",
    "DeterministicSummaryFallback",
    "FailCompactionFinal",
    "FullJitterCompactionRetryPolicy",
    "RetryCompactionAt",
    "RetryableCompactionError",
    "UseDeterministicFallback",
]
