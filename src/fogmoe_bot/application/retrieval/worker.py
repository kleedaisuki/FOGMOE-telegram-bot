"""@brief Conversation 情景投影与 embedding 的 durable worker / Durable worker for episodic projection and embeddings."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

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
from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
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
        poll_interval: float = 0.5,
        lease_for: timedelta = timedelta(minutes=2),
        max_attempts: int = 5,
        clock: UtcClock | None = None,
        jitter: Jitter = random.uniform,
    ) -> None:
        """@brief 创建 retrieval worker / Create the retrieval worker.

        @raise ValueError worker 配置非法 / Invalid worker configuration.
        """

        if worker_count < 1:
            raise ValueError("Retrieval worker_count must be positive")
        if not 1 <= batch_size <= 128:
            raise ValueError("Retrieval batch_size must be between 1 and 128")
        if poll_interval <= 0.0 or not math.isfinite(poll_interval):
            raise ValueError("Retrieval poll_interval must be finite and positive")
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
        self._poll_interval = poll_interval
        self._lease_for = lease_for
        self._max_attempts = max_attempts
        self._clock = clock or SystemUtcClock()
        self._jitter = jitter

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行到停止并排空已领取 batch / Run until stopped and drain claimed batches.

        @param stop_event 顶层运行时停止信号 / Top-level runtime stop signal.
        @return None / None.
        """

        await self._store.ensure_space(self._space)
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
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(
                self._run_projector(stop_event),
                name="retrieval-projection",
            )
            for ordinal in range(self._worker_count):
                task_group.create_task(
                    self._run_vector_consumer(stop_event),
                    name=f"retrieval-vector:{ordinal}",
                )

    async def _run_projector(self, stop_event: asyncio.Event) -> None:
        """@brief 运行唯一 source projection producer / Run the sole source-projection producer.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            did_work = await self._project_sources()
            if not did_work:
                await _wait_or_stop(stop_event, self._poll_interval)

    async def _project_sources(self) -> bool:
        """@brief 投影一个来源批次 / Project one source batch.

        @return 本轮是否发现来源 / Whether this cycle found sources.
        """

        now = self._clock.now()
        sources = await self._source.read_unprojected(
            format_version=self._renderer.format_version,
            limit=self._batch_size,
        )
        for source in sources:
            await self._store.project_turn(
                source,
                self._renderer.render(source),
                space=self._space,
                projected_at=now,
            )
            self._telemetry.counter(
                MetricName.RETRIEVAL_OUTCOMES,
                attributes={"operation": "projection", "outcome": Outcome.SUCCESS},
            )
        return bool(sources)

    async def _run_vector_consumer(self, stop_event: asyncio.Event) -> None:
        """@brief 运行一个只领取 vector intent 的 consumer / Run one consumer owning vector intents only.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            did_work = await self._process_vector_batch()
            if not did_work:
                await _wait_or_stop(stop_event, self._poll_interval)

    async def _process_vector_batch(self) -> bool:
        """@brief 领取并处理一个 vector batch / Claim and process one vector batch.

        @return 是否领取到任务 / Whether any vector intents were claimed.
        """

        now = self._clock.now()
        claims = await self._store.claim_vectors(
            space=self._space,
            now=now,
            limit=self._batch_size,
            lease_for=self._lease_for,
        )
        if claims:
            await self._embed_claims(claims)
        return bool(claims)

    async def _embed_claims(self, claims: Sequence[PassageVectorClaim]) -> None:
        """@brief 单次 Provider batch 后逐条 fenced 完成 / Complete claims individually after one provider batch.

        @param claims 同一空间 claims / Claims from one embedding space.
        @return None / None.
        """

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


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> None:
    """@brief 等待 poll 周期或停止信号 / Wait for a poll interval or stop signal.

    @return None / None.
    """

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        return


__all__ = ["RetrievalWorker"]
