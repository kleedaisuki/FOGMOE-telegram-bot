"""@brief Runtime-owned durable Dreaming worker / Runtime-owned durable Dreaming worker."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta

from fogmoe_bot.application.observability.telemetry import SpanScope, Telemetry
from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    Jitter,
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.domain.user_profile import (
    DreamClaim,
    DreamCompletionPrepared,
    DreamFailure,
    StaleDreamClaimError,
)

from .ports import (
    DreamCommitReceipt,
    DreamProfileUnchanged,
    DreamProfileUpdated,
    DreamingModel,
    ProfileEvidenceSource,
    ProfileStore,
    RetryableDreamingError,
)

logger = logging.getLogger(__name__)
"""@brief Dreaming worker logger / Dreaming-worker logger."""


class _NoOpDreamSpan:
    """@brief observability 不可用时的无副作用 span / Side-effect-free span used when observability is unavailable."""

    def set_attribute(self, key: str, value: object) -> None:
        """@brief 丢弃 span 属性且不影响业务 / Discard a span attribute without affecting business work.

        @param key 属性键 / Attribute key.
        @param value 属性值 / Attribute value.
        @return None / None.
        """

        del key, value


_NOOP_DREAM_SPAN = _NoOpDreamSpan()
"""@brief Dreaming 的 fail-open span / Fail-open span for Dreaming."""


@contextmanager
def _best_effort_dream_span(
    telemetry: Telemetry,
    claim: DreamClaim,
) -> Iterator[SpanScope | _NoOpDreamSpan]:
    """@brief 隔离 span 生命周期故障并保留原始业务异常 / Isolate span-lifecycle faults while preserving the original business exception.

    @param telemetry observability recorder / Observability recorder.
    @param claim 当前冻结 Dream claim / Current frozen Dream claim.
    @return 已进入的真实 span；不可用时返回 no-op span /
        Entered real span, or a no-op span when unavailable.
    @note span 进入失败不得阻止 provider；span 退出失败不得覆盖业务错误或改变
        settlement 分类。/ Span-entry failure must not block the provider; span-exit failure
        must not mask a business error or change settlement classification.
    """

    try:
        scope = telemetry.span(
            "user_profile.dream",
            kind=SpanKind.CONSUMER,
            attributes={
                "user_profile.owner_user_id": claim.activity.owner_user_id,
                "user_profile.base_revision": claim.activity.baseline.revision,
                "user_profile.evidence.count": len(claim.evidence),
            },
        )
        span = scope.__enter__()
    except Exception:
        logger.exception(
            "Dreaming span could not start; processing without tracing: dream_id=%s",
            claim.activity.dream_id,
        )
        yield _NOOP_DREAM_SPAN
        return

    try:
        yield span
    except BaseException as error:
        try:
            scope.__exit__(type(error), error, error.__traceback__)
        except Exception:
            logger.exception(
                "Dreaming span could not record a business failure: dream_id=%s",
                claim.activity.dream_id,
            )
        raise
    else:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            logger.exception(
                "Dreaming span could not finish: dream_id=%s",
                claim.activity.dream_id,
            )


class DreamingWorker:
    """@brief 单 coordinator 与固定模型 consumers 的 Profile consolidation / Profile consolidation with one coordinator and fixed model consumers."""

    def __init__(
        self,
        *,
        source: ProfileEvidenceSource,
        store: ProfileStore,
        model: DreamingModel,
        telemetry: Telemetry,
        polling_policy: AdaptivePollingPolicy,
        worker_count: int = 2,
        batch_size: int = 8,
        source_batch_size: int = 32,
        max_events_per_dream: int = 64,
        max_evidence_chars: int = 60_000,
        refresh_after: timedelta = timedelta(hours=6),
        attempt_timeout: timedelta = timedelta(seconds=90),
        lease_for: timedelta = timedelta(seconds=120),
        max_attempts: int = 5,
        clock: UtcClock | None = None,
        jitter: Jitter = random.uniform,
        recovery_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """@brief 创建 Dreaming worker / Create a Dreaming worker.

        @param source 未投影 evidence 来源 / Source of unprojected evidence.
        @param store durable Profile/Dream store / Durable Profile and Dream store.
        @param model 结构化 Dreaming 模型 / Structured Dreaming model.
        @param telemetry typed observability 出口 / Typed observability sink.
        @param polling_policy coordinator 与每个 consumer 共享策略、独立状态的轮询配置 /
            Polling configuration shared as a policy but instantiated independently by the
            coordinator and every consumer.
        @param worker_count 固定 consumer 数 / Fixed consumer count.
        @param batch_size 单轮 enqueue 上限 / Enqueue limit per pass.
        @param source_batch_size 单轮来源投影上限 / Source-projection limit per pass.
        @param max_events_per_dream 单个 Dream 最大 evidence 数 / Maximum evidence items per Dream.
        @param max_evidence_chars 单个 Dream 最大文本字符数 / Maximum text characters per Dream.
        @param refresh_after idle Profile 刷新间隔 / Idle-Profile refresh interval.
        @param attempt_timeout 单次模型生成 deadline / Per-attempt model deadline.
        @param lease_for worker ownership 租约 / Worker ownership lease.
        @param max_attempts 自动重试上限 / Automatic retry limit.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param jitter retry jitter 函数 / Retry-jitter function.
        @param recovery_monotonic lease recovery cadence 的可替换单调时钟 /
            Replaceable monotonic clock for the lease-recovery cadence.
        @return None / None.
        @raise ValueError 任一容量或时间预算非法 / Invalid capacity or time budget.
        """

        if worker_count < 1:
            raise ValueError("Dreaming worker_count must be positive")
        if not 1 <= batch_size <= 64:
            raise ValueError("Dreaming batch_size must be between 1 and 64")
        if not 1 <= source_batch_size <= 128:
            raise ValueError("Dreaming source_batch_size must be between 1 and 128")
        if not 1 <= max_events_per_dream <= 256:
            raise ValueError("Dreaming max_events_per_dream must be between 1 and 256")
        if not 4_096 <= max_evidence_chars <= 1_000_000:
            raise ValueError(
                "Dreaming max_evidence_chars must be between 4096 and 1000000"
            )
        if refresh_after <= timedelta():
            raise ValueError("Dreaming refresh_after must be positive")
        if attempt_timeout <= timedelta() or lease_for <= attempt_timeout:
            raise ValueError("Dreaming lease must outlive its positive attempt timeout")
        if max_attempts < 1:
            raise ValueError("Dreaming max_attempts must be positive")
        self._source = source
        self._store = store
        self._model = model
        self._telemetry = telemetry
        self._worker_count = worker_count
        self._batch_size = batch_size
        self._source_batch_size = source_batch_size
        self._max_events_per_dream = max_events_per_dream
        self._max_evidence_chars = max_evidence_chars
        self._polling_policy = polling_policy
        self._refresh_after = refresh_after
        self._attempt_timeout = attempt_timeout
        self._lease_for = lease_for
        self._max_attempts = max_attempts
        self._clock = clock or SystemUtcClock()
        self._jitter = jitter
        self._recovery_monotonic = recovery_monotonic

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行至停止并排空已领取 claim / Run until stopped and drain claimed work.

        @param stop_event 顶层 structured stop / Top-level structured stop.
        @return None / None.
        """

        recovery = LeaseRecoveryCadence.for_lease(
            self._lease_for,
            monotonic=self._recovery_monotonic,
        )
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(
                self._recover_leases(stop_event, recovery=recovery),
                name="dreaming-recovery",
            )
            task_group.create_task(
                self._run_coordinator(stop_event),
                name="dreaming-coordinator",
            )
            for ordinal in range(self._worker_count):
                task_group.create_task(
                    self._run_consumer(stop_event),
                    name=f"dreaming-model:{ordinal}",
                )

    async def _run_coordinator(
        self,
        stop_event: asyncio.Event,
    ) -> None:
        """@brief 唯一负责 source discovery 与 job formation / Sole owner of source discovery and job formation.

        @param stop_event 顶层结构化停止信号 / Top-level structured stop signal.
        @return None / None.
        """

        polling = self._polling_policy.start()
        while not stop_event.is_set():
            try:
                did_work = await self._coordinate_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dreaming coordinator pass failed; will retry")
                did_work = False
            if did_work:
                polling.reset()
                continue
            await polling.wait(stop_event)

    async def _recover_leases(
        self,
        stop_event: asyncio.Event,
        *,
        recovery: LeaseRecoveryCadence,
    ) -> None:
        """@brief 以单一 owner 独立回收过期 Dream leases / Recover expired Dream leases independently under one owner.

        @param stop_event 顶层结构化停止信号 / Top-level structured stop signal.
        @param recovery 与 lease 生命周期对齐的恢复节奏 / Recovery cadence aligned with the lease lifecycle.
        @return None；恢复故障不会取消 coordinator 或 consumers /
            None; recovery failures do not cancel the coordinator or consumers.
        @note 首次循环立即恢复；后续等待直接竞争 stop event，不受业务轮询退避影响。/
            The first loop recovers immediately; later waits race the stop event and are
            independent of business-polling backoff.
        """

        while not stop_event.is_set():
            if recovery.take_due():
                await self._recover_expired_leases()
            try:
                async with asyncio.timeout(recovery.interval_seconds):
                    await stop_event.wait()
            except TimeoutError:
                continue

    async def _coordinate_once(self) -> bool:
        """@brief 投影一批证据并调度到期 Profile / Project evidence and schedule due Profiles once.

        @return 是否发现或建立工作 / Whether work was discovered or formed.
        """

        sources = await self._source.read_unprojected(limit=self._source_batch_size)
        if sources:
            with self._telemetry.span(
                "user_profile.evidence.projection",
                kind=SpanKind.CONSUMER,
                attributes={"user_profile.evidence.count": len(sources)},
            ):
                projected_at = self._clock.now()
                for evidence in sources:
                    await self._store.project_evidence(
                        evidence,
                        projected_at=projected_at,
                    )
        enqueued = await self._store.enqueue_eligible(
            now=self._clock.now(),
            limit=self._batch_size,
            max_events_per_dream=self._max_events_per_dream,
            max_evidence_chars=self._max_evidence_chars,
        )
        if enqueued:
            self._telemetry.counter(
                MetricName.USER_PROFILE_OUTCOMES,
                float(enqueued),
                attributes={"operation": "enqueue", "outcome": Outcome.SUCCESS},
            )
        return bool(sources or enqueued)

    async def _run_consumer(self, stop_event: asyncio.Event) -> None:
        """@brief 只消费 durable Dream jobs / Consume only durable Dream jobs.

        @param stop_event cooperative shutdown 信号 / Cooperative-shutdown signal.
        @return None / None.
        """

        polling = self._polling_policy.start()
        while not stop_event.is_set():
            try:
                claims = await self._store.claim_dreams(
                    now=self._clock.now(),
                    limit=1,
                    lease_for=self._lease_for,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dreaming claim pass failed; will retry")
                await polling.wait(stop_event)
                continue
            if not claims:
                await polling.wait(stop_event)
                continue
            polling.reset()
            for claim in claims:
                try:
                    await self._process(claim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Dreaming claim could not be finalized: dream_id=%s",
                        claim.activity.dream_id,
                    )

    async def _recover_expired_leases(self) -> None:
        """@brief 尝试回收已过期 Dream 租约 / Attempt to recover expired Dream leases.

        @return None / None.
        @note 存储层只回收 ``lease_expires_at <= now`` 的 claim；回收会清除
            token，之后的 reclaim 会安装新 token，fencing 才会拒绝旧 owner。
            lease 时间到期本身不会使 token 失效。attempt timeout 严格小于 lease，
            所以不晚于半租约的扫描不会抢走仍在有效租约内的 claim。/
            The store recovers only claims whose ``lease_expires_at <= now``. Recovery clears the
            token, and a later reclaim installs a new token; only then does fencing reject the old
            owner. Passage of the lease deadline alone does not invalidate a token. The attempt
            timeout is strictly shorter than the lease, so a cadence no later than half the lease
            cannot steal a claim whose lease remains valid.
        """

        try:
            recovered = await self._store.recover_expired_dream_leases(
                now=self._clock.now(),
                max_attempts=self._max_attempts,
                limit=self._batch_size,
            )
            if recovered:
                self._telemetry.counter(
                    MetricName.LEASE_RECOVERIES,
                    float(recovered),
                    attributes={"pipeline.stage": "user_profile.dreaming"},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dreaming lease recovery failed; a later pass will retry")

    async def _process(self, claim: DreamClaim) -> None:
        """@brief 分离 pre-settlement 与 outcome-unknown/committed phases / Separate pre-settlement from outcome-unknown and committed phases.

        @param claim 冻结 claim / Frozen claim.
        @return None / None.
        @note 一旦进入 store settlement，异常可能是 commit acknowledgment 丢失；此时
            不得再对同一 claim 执行 retry/fail。/ Once store settlement starts, an exception
            may mean that only the commit acknowledgment was lost; retry/fail must not be applied
            to the same claim afterward.
        """

        settlement_started = False
        try:
            with _best_effort_dream_span(self._telemetry, claim) as span:
                if claim.activity.attempt_count > self._max_attempts:
                    failure = claim.record_failure(
                        failed_at=self._clock.now(),
                        failure=DreamFailure(
                            "Dream claim exceeded the configured attempt budget"
                        ),
                    ).fail_final()
                    settlement_started = True
                    await self._store.fail_dream(failure)
                    span.set_attribute(
                        "user_profile.result",
                        "attempt_budget_exhausted",
                    )
                    self._telemetry.counter(
                        MetricName.USER_PROFILE_OUTCOMES,
                        attributes={
                            "operation": "dream",
                            "outcome": Outcome.FAILURE,
                            "result": "attempt_budget_exhausted",
                        },
                    )
                    return
                async with asyncio.timeout(self._attempt_timeout.total_seconds()):
                    result = await self._model.dream(claim)
                try:
                    evaluated = claim.evaluate_result(result)
                    completion = evaluated.prepare(
                        completed_at=self._clock.now(),
                    )
                except ValueError as error:
                    raise RetryableDreamingError(
                        f"Dreaming patch violated domain invariants: {error}"
                    ) from error
                settlement_started = True
                receipt = await self._store.complete_dream(
                    completion,
                    refresh_after=self._refresh_after,
                )
                result_label = self._result_label(completion, receipt)
                span.set_attribute(
                    "user_profile.result",
                    result_label,
                )
                self._telemetry.counter(
                    MetricName.USER_PROFILE_OUTCOMES,
                    attributes={
                        "operation": "dream",
                        "outcome": Outcome.SUCCESS,
                        "result": result_label,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if settlement_started:
                logger.exception(
                    "Dreaming settlement was not acknowledged; fencing or lease recovery "
                    "will converge dream_id=%s",
                    claim.activity.dream_id,
                )
                return
            if isinstance(error, StaleDreamClaimError):
                logger.info(
                    "Discarded stale Dreaming claim dream_id=%s",
                    claim.activity.dream_id,
                )
                return
            await self._handle_failure(claim, error)

    @staticmethod
    def _result_label(
        completion: DreamCompletionPrepared,
        receipt: DreamCommitReceipt,
    ) -> str:
        """@brief 校验 committed receipt 与准备决定一致并映射低基数标签 / Validate a committed receipt against its prepared decision and map a low-cardinality label.

        @param completion 已提交的 Dream completion 决定 / Committed Dream-completion decision.
        @param receipt store 返回的 durable 后态回执 / Durable post-state receipt returned by the store.
        @return ``updated`` 或 ``no_op`` / ``updated`` or ``no_op``.
        @raise RuntimeError 回执 discriminator 或语义与决定不一致 / Receipt discriminator or semantics disagree with the decision.
        """

        claim = completion.claim
        if isinstance(receipt, DreamProfileUpdated):
            snapshot = receipt.snapshot
            result = completion.activity.result
            completed_at = completion.activity.completed_at
            if (
                not completion.changed
                or result is None
                or completed_at is None
                or snapshot.user_id != claim.activity.owner_user_id
                or snapshot.revision != claim.activity.baseline.revision + 1
                or snapshot.document != completion.document
                or snapshot.observed_through_event_id != claim.activity.through_event_id
                or snapshot.updated_at != completed_at
                or snapshot.route_key != result.route_key
                or snapshot.prompt_version != result.prompt_version
            ):
                raise RuntimeError(
                    "Updated Dream receipt disagreed with its domain decision"
                )
            return "updated"
        if isinstance(receipt, DreamProfileUnchanged):
            if (
                completion.changed
                or receipt.owner_user_id != claim.activity.owner_user_id
                or receipt.retained_revision != claim.activity.baseline.revision
                or receipt.scheduler_head_event_id != claim.activity.through_event_id
            ):
                raise RuntimeError(
                    "NO_OP Dream receipt disagreed with its domain decision"
                )
            return "no_op"
        raise TypeError("Dream store returned an unknown commit receipt")

    async def _handle_failure(self, claim: DreamClaim, error: Exception) -> None:
        """@brief 将失败分类为有限 retry 或 final / Classify a failure into bounded retry or final failure.

        @param claim 失败 claim / Failed claim.
        @param error 原始错误 / Original error.
        @return None / None.
        """

        failed_at = self._clock.now()
        detail = f"{error.__class__.__name__}: {error}"[:1000]
        failure = claim.record_failure(
            failed_at=failed_at,
            failure=DreamFailure(detail),
        )
        retryable = isinstance(error, RetryableDreamingError | TimeoutError)
        if retryable and claim.activity.attempt_count < self._max_attempts:
            retry_after = (
                error.retry_after if isinstance(error, RetryableDreamingError) else None
            )
            cap = min(
                300.0,
                2.0 * (2 ** max(0, claim.activity.attempt_count - 1)),
            )
            sampled = self._jitter(0.0, cap)
            if not math.isfinite(sampled) or not 0.0 <= sampled <= cap:
                raise ValueError("Dreaming jitter returned an invalid sample")
            delay = max(sampled, 0.001)
            if retry_after is not None:
                delay = max(delay, retry_after.total_seconds())
            await self._store.retry_dream(
                failure.schedule_retry(
                    retry_at=failed_at + timedelta(seconds=delay),
                ),
            )
            outcome = Outcome.RETRY
        else:
            await self._store.fail_dream(failure.fail_final())
            outcome = Outcome.FAILURE
        self._telemetry.counter(
            MetricName.USER_PROFILE_OUTCOMES,
            attributes={"operation": "dream", "outcome": outcome},
        )
        logger.warning(
            "Dreaming job failed dream_id=%s attempt=%s outcome=%s error=%s",
            claim.activity.dream_id,
            claim.activity.attempt_count,
            outcome,
            detail,
        )


__all__ = ["DreamingWorker"]
