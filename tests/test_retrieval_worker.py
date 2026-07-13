"""@brief Durable Retrieval worker 测试 / Tests for the durable Retrieval worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fogmoe_bot.application.retrieval import (
    EpisodicPassageRenderer,
    EpisodicTurn,
    PassageVectorClaim,
    RetrievalWorker,
    RetryableEmbeddingError,
)
from fogmoe_bot.application.runtime import UtcClock
from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
)


NOW = datetime(2034, 1, 1, tzinfo=UTC)
"""@brief 确定性 worker 时间 / Deterministic worker instant."""


class _Clock(UtcClock):
    """@brief 固定 UTC clock / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed instant."""

        return NOW


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
        passages: tuple[RetrievalPassage, ...],
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
        owner_user_id: int,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief Worker 测试不执行 search / Worker tests do not execute search."""

        raise AssertionError((owner_user_id, corpus_id, space, query_vector, limit))


class _Embeddings:
    """@brief 可切换成功/重试的 embedding fake / Embedding fake switching between success and retry."""

    def __init__(self, *, fail: bool) -> None:
        """@brief 保存失败开关 / Store the failure switch."""

        self._fail = fail

    async def embed_documents(
        self,
        texts: tuple[str, ...],
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


def _worker(
    *, fail: bool, stop_event: asyncio.Event
) -> tuple[RetrievalWorker, _Store, _Source]:
    """@brief 构造固定 worker 场景 / Build a fixed worker scenario."""

    turn = EpisodicTurn(
        turn_id=UUID("00000000-0000-0000-0000-000000000042"),
        owner_user_id=42,
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
    worker = RetrievalWorker(
        source=source,
        store=store,
        embeddings=_Embeddings(fail=fail),
        space=space,
        renderer=EpisodicPassageRenderer(),
        telemetry=Telemetry(TelemetryBuffer(32)),
        worker_count=4,
        batch_size=4,
        poll_interval=0.01,
        lease_for=timedelta(seconds=30),
        clock=_Clock(),
    )
    return worker, store, source


def test_worker_projects_embeds_and_drains_structurally() -> None:
    """@brief Worker 在一个 owned TaskGroup 中完成 projection 与 embedding / Worker completes projection and embedding in one owned TaskGroup."""

    async def scenario() -> None:
        """@brief 执行成功场景 / Execute the success scenario."""

        stop_event = asyncio.Event()
        worker, store, source = _worker(fail=False, stop_event=stop_event)
        await worker.run(stop_event)
        assert store.completed == EmbeddingVector((1.0, 0.0))
        assert store.retried_at is None
        assert source.reader_tasks
        assert set(source.reader_tasks) == {"retrieval-projection"}

    asyncio.run(scenario())


def test_worker_honors_provider_retry_after() -> None:
    """@brief Retry-After 直接成为 durable retry 下界 / Retry-After becomes the durable retry boundary."""

    async def scenario() -> None:
        """@brief 执行 retry 场景 / Execute the retry scenario."""

        stop_event = asyncio.Event()
        worker, store, source = _worker(fail=True, stop_event=stop_event)
        await worker.run(stop_event)
        assert store.completed is None
        assert store.retried_at == NOW + timedelta(seconds=7)
        assert set(source.reader_tasks) == {"retrieval-projection"}

    asyncio.run(scenario())
