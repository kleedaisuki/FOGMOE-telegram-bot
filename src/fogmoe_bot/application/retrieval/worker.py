"""@brief Conversation 情景投影与 embedding 的 durable worker / Durable worker for episodic projection and embeddings."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.retrieval.episodic import EpisodicPassageRenderer
from fogmoe_bot.application.retrieval.ports import (
    EmbeddingContractError,
    EmbeddingProvider,
    EpisodicSource,
    PassageVectorClaim,
    RetrievalStore,
    RetryableEmbeddingError,
    StaleVectorClaimError,
)
from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    LeaseRecoveryCadence,
    SystemUtcClock,
    UtcClock,
)
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus
from fogmoe_bot.domain.retrieval import EmbeddingSpace, EmbeddingVector

logger = logging.getLogger(__name__)
"""@brief Retrieval worker logger / Retrieval-worker logger."""

type Jitter = Callable[[float, float], float]
"""@brief 可注入 full-jitter 随机源 / Injectable full-jitter random source."""


class RetrievalWorker:
    """@brief 自愈 source projection 与有界 vector worker / Self-healing source projection and bounded vector worker.

    @param source 未投影情景来源 / Unprojected episodic source.
    @param store 检索持久化 / Retrieval persistence.
    @param embeddings Provider adapter / Provider adapter.
    @param space 活跃嵌入空间 / Active embedding space.
    @param renderer Passage renderer / Passage renderer.
    """

    def __init__(
        self,
        *,
        source: EpisodicSource,
        store: RetrievalStore,
        embeddings: EmbeddingProvider,
        space: EmbeddingSpace,
        renderer: EpisodicPassageRenderer,
        telemetry: Telemetry,
        worker_count: int = 2,
        batch_size: int = 16,
        polling_policy: AdaptivePollingPolicy,
        lease_for: timedelta = timedelta(minutes=2),
        max_attempts: int = 5,
        clock: UtcClock | None = None,
        jitter: Jitter = random.uniform,
        recovery_monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """@brief 创建 retrieval worker / Create the retrieval worker.

        @param recovery_monotonic lease recovery cadence 的可替换单调时钟 / Replaceable monotonic clock for the lease-recovery cadence.
        @raise ValueError worker 配置非法 / Invalid worker configuration.
        """

        if worker_count < 1:
            raise ValueError("Retrieval worker_count must be positive")
        if not 1 <= batch_size <= 128:
            raise ValueError("Retrieval batch_size must be between 1 and 128")
        if lease_for <= timedelta():
            raise ValueError("Retrieval lease_for must be positive")
        if max_attempts < 1:
            raise ValueError("Retrieval max_attempts must be positive")
        if renderer.format_version != space.passage_format_version:
            raise ValueError("Renderer and embedding-space format versions must match")
        self._source = source
        self._store = store
        self._embeddings = embeddings
        self._space = space
        self._renderer = renderer
        self._telemetry = telemetry
        self._worker_count = worker_count
        self._batch_size = batch_size
        self._polling_policy = polling_policy
        self._lease_for = lease_for
        self._max_attempts = max_attempts
        self._clock = clock or SystemUtcClock()
        self._jitter = jitter
        self._recovery_monotonic = recovery_monotonic

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行到停止并排空已领取 batch / Run until stopped and drain claimed batches.

        @param stop_event 顶层运行时停止信号 / Top-level runtime stop signal.
        @return None / None.
        """

        initialized = await self._initialize(stop_event)
        if not initialized:
            return
        recovery = LeaseRecoveryCadence.for_lease(
            self._lease_for,
            monotonic=self._recovery_monotonic,
        )
        if recovery.take_due():
            await self._recover_expired_leases()
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(
                self._run_projector(stop_event, recovery=recovery),
                name="retrieval-projection",
            )
            for ordinal in range(self._worker_count):
                task_group.create_task(
                    self._run_vector_consumer(stop_event),
                    name=f"retrieval-vector:{ordinal}",
                )

    async def _initialize(self, stop_event: asyncio.Event) -> bool:
        """@brief 重试建立 embedding 空间，直至可运行或收到停止 / Retry embedding-space initialization until runnable or stopped.

        @param stop_event 顶层停止信号 / Top-level stop signal.
        @return 空间已就绪时为 True，停止先到时为 False / True when the space is ready; False when stop arrives first.
        @note ``ensure_space`` 触及数据库与 telemetry hook；临时失败必须不影响其余
            BotRuntime 服务。/ ``ensure_space`` touches the database and telemetry hooks, so
            transient failures must not affect other BotRuntime services.
        """

        polling = self._polling_policy.start()
        while not stop_event.is_set():
            try:
                await self._store.ensure_space(self._space)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Retrieval embedding-space initialization failed; will retry"
                )
                await polling.wait(stop_event)
                continue
            return True
        return False

    async def _run_projector(
        self,
        stop_event: asyncio.Event,
        *,
        recovery: LeaseRecoveryCadence,
    ) -> None:
        """@brief 运行唯一 source projection producer / Run the sole source-projection producer.

        @param stop_event 停止信号 / Stop signal.
        @param recovery 此 worker 唯一的 lease recovery cadence / Sole lease-recovery cadence for this worker.
        @return None / None.
        """

        polling = self._polling_policy.start()
        while not stop_event.is_set():
            if recovery.take_due():
                await self._recover_expired_leases()
            try:
                did_work = await self._project_sources()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retrieval source-projection pass failed; will retry")
                did_work = False
            if did_work:
                polling.reset()
                continue
            await polling.wait(stop_event)

    async def _project_sources(self) -> bool:
        """@brief 投影一个来源批次 / Project one source batch.

        @return 本轮是否发现来源 / Whether this cycle found sources.
        """

        now = self._clock.now()
        started_ns = time.perf_counter_ns()
        sources = await self._source.read_unprojected(
            format_version=self._renderer.format_version,
            limit=self._batch_size,
        )
        discovery_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
        if not sources:
            return False
        self._telemetry.gauge(
            MetricName.RETRIEVAL_SOURCE_DISCOVERY_DURATION,
            discovery_seconds,
            unit="s",
        )
        try:
            with self._telemetry.span(
                "retrieval.projection.batch",
                kind=SpanKind.CONSUMER,
                attributes={
                    "retrieval.space.id": self._space.space_id,
                    "retrieval.batch.limit": self._batch_size,
                },
            ) as span:
                passage_count = 0
                for source in sources:
                    passages = self._renderer.render(source)
                    passage_count += len(passages)
                    await self._store.project_turn(
                        source,
                        passages,
                        space=self._space,
                        projected_at=now,
                    )
                    self._telemetry.counter(
                        MetricName.RETRIEVAL_OUTCOMES,
                        attributes={
                            "operation": "projection",
                            "outcome": Outcome.SUCCESS,
                        },
                    )
                span.set_attribute("retrieval.source.count", len(sources))
                span.set_attribute("retrieval.passage.count", passage_count)
        except Exception:
            self._telemetry.counter(
                MetricName.RETRIEVAL_OUTCOMES,
                attributes={
                    "operation": "projection",
                    "outcome": Outcome.FAILURE,
                },
            )
            raise
        self._telemetry.gauge(
            MetricName.RETRIEVAL_BATCH_SIZE,
            float(len(sources)),
            unit="{source}",
            attributes={"stage": "projection"},
        )
        return bool(sources)

    async def _run_vector_consumer(self, stop_event: asyncio.Event) -> None:
        """@brief 运行一个只领取 vector intent 的 consumer / Run one consumer owning vector intents only.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        polling = self._polling_policy.start()
        while not stop_event.is_set():
            try:
                did_work = await self._process_vector_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retrieval vector-consumer pass failed; will retry")
                did_work = False
            if did_work:
                polling.reset()
                continue
            await polling.wait(stop_event)

    async def _recover_expired_leases(self) -> None:
        """@brief 回收当前空间已经过期的 embedding 租约 / Recover expired embedding leases in the active space.

        @return None / None.
        @note production composition 的 embedding HTTP total timeout 严格短于 lease；
            storage 只回收 ``lease_expires_at <= now`` 的 processing row。回收清除
            token，之后的 reclaim 安装新 token，fencing 才会拒绝旧 owner；
            lease 时间到期本身不会使 token 失效。因此周期扫描不会抢走仍在
            有效 lease 内的请求。/ Production composition keeps the embedding HTTP
            total timeout strictly below the lease. Storage recovers only processing rows with
            ``lease_expires_at <= now``. Recovery clears the token and a later reclaim installs a
            new token; only then does fencing reject the old owner. Passage of the lease deadline
            alone does not invalidate a token, so periodic scans do not steal requests whose
            leases remain valid.
        """

        try:
            recovered = await self._store.recover_expired_vector_leases(
                space=self._space,
                now=self._clock.now(),
            )
            if recovered:
                self._telemetry.counter(
                    MetricName.LEASE_RECOVERIES,
                    float(recovered),
                    attributes={"pipeline.stage": "retrieval"},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Retrieval lease recovery failed; projection and vector claims continue"
            )

    async def _process_vector_batch(self) -> bool:
        """@brief 领取并处理一个 vector batch / Claim and process one vector batch.

        @return 是否领取到任务 / Whether any vector intents were claimed.
        """

        now = self._clock.now()
        started_ns = time.perf_counter_ns()
        claims = await self._store.claim_vectors(
            space=self._space,
            now=now,
            limit=self._batch_size,
            lease_for=self._lease_for,
        )
        claim_seconds = (time.perf_counter_ns() - started_ns) / 1_000_000_000
        if not claims:
            return False
        self._telemetry.gauge(
            MetricName.RETRIEVAL_VECTOR_CLAIM_DURATION,
            claim_seconds,
            unit="s",
        )
        await self._embed_claims(claims)
        self._telemetry.gauge(
            MetricName.RETRIEVAL_BATCH_SIZE,
            float(len(claims)),
            unit="{passage}",
            attributes={"stage": "embedding"},
        )
        return True

    async def _embed_claims(self, claims: Sequence[PassageVectorClaim]) -> None:
        """@brief 单次 Provider batch 后逐条 fenced 完成 / Complete claims individually after one provider batch.

        @param claims 同一空间 claims / Claims from one embedding space.
        @return None / None.
        """

        with self._telemetry.span(
            "retrieval.embedding.batch",
            kind=SpanKind.INTERNAL,
            attributes={
                "retrieval.space.id": self._space.space_id,
                "retrieval.batch.size": len(claims),
                "retrieval.embedding.model": self._space.model,
                "retrieval.embedding.dimensions": self._space.dimensions,
            },
        ) as span:
            try:
                vectors = tuple(
                    await self._embeddings.embed_documents(
                        tuple(claim.passage.text for claim in claims),
                        space=self._space,
                    )
                )
                if len(vectors) != len(claims):
                    raise EmbeddingContractError(
                        "Embedding provider returned a different batch length"
                    )
                for claim, vector in zip(claims, vectors, strict=True):
                    await self._complete_claim(claim, vector)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                span.set_status(SpanStatus.ERROR, str(error))
                span.set_attribute("error.type", type(error).__name__)
                await self._handle_batch_failure(claims, error)

    async def _complete_claim(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
    ) -> None:
        """@brief 完成仍有效的单个 claim / Complete one claim if it remains current.

        @return None / None.
        """

        try:
            vector.require_space(self._space)
            await self._store.complete_vector(
                claim,
                vector,
                completed_at=self._clock.now(),
            )
            self._telemetry.counter(
                MetricName.RETRIEVAL_OUTCOMES,
                attributes={"operation": "embedding", "outcome": Outcome.SUCCESS},
            )
        except StaleVectorClaimError:
            logger.info(
                "Retrieval vector claim became stale passage_id=%s",
                claim.passage.passage_id,
            )

    async def _handle_batch_failure(
        self,
        claims: Sequence[PassageVectorClaim],
        error: Exception,
    ) -> None:
        """@brief 将 batch 失败分类为 retry 或 final / Classify a batch failure as retry or final.

        @return None / None.
        """

        failed_at = self._clock.now()
        safe_error = f"{type(error).__name__}: {str(error)[:400]}"
        for claim in claims:
            try:
                if (
                    isinstance(error, EmbeddingContractError)
                    or claim.attempt_count >= self._max_attempts
                ):
                    await self._store.fail_vector(
                        claim,
                        error=safe_error,
                        failed_at=failed_at,
                    )
                    self._telemetry.counter(
                        MetricName.RETRIEVAL_OUTCOMES,
                        attributes={
                            "operation": "embedding",
                            "outcome": Outcome.FAILURE,
                        },
                    )
                    continue
                await self._store.retry_vector(
                    claim,
                    retry_at=self._retry_at(claim, error, failed_at),
                    error=safe_error,
                    failed_at=failed_at,
                )
                self._telemetry.counter(
                    MetricName.RETRIEVAL_OUTCOMES,
                    attributes={"operation": "embedding", "outcome": Outcome.RETRY},
                )
            except StaleVectorClaimError:
                logger.info(
                    "Retrieval failure ignored for stale passage_id=%s",
                    claim.passage.passage_id,
                )
        logger.warning(
            "Retrieval embedding batch failed size=%s error_type=%s",
            len(claims),
            type(error).__name__,
        )

    def _retry_at(
        self,
        claim: PassageVectorClaim,
        error: Exception,
        failed_at: datetime,
    ) -> datetime:
        """@brief 计算 full-jitter retry 时刻 / Compute a full-jitter retry instant.

        @return 严格晚于失败时刻的时间 / Instant strictly after failure.
        """

        retry_after = (
            error.retry_after if isinstance(error, RetryableEmbeddingError) else None
        )
        if retry_after is not None:
            return failed_at + retry_after
        cap = min(300.0, 2.0 * (2 ** max(0, claim.attempt_count - 1)))
        sampled = self._jitter(0.0, cap)
        if not math.isfinite(sampled) or not 0.0 <= sampled <= cap:
            raise ValueError("Retrieval jitter returned an invalid sample")
        return failed_at + timedelta(seconds=max(sampled, 0.001))


__all__ = ["RetrievalWorker"]
