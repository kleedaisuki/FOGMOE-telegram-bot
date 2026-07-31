"""@brief PostgreSQL Retrieval adapter 的 durable vector 测试 / Durable-vector tests for the PostgreSQL Retrieval adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    PassageVectorJob,
    PassageVectorJobKey,
    RECOVERED_PASSAGE_VECTOR_LEASE_ERROR,
    RetrievalPassage,
    RetrievalScope,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.retrieval import PostgresRetrievalStore

NOW = datetime(2036, 2, 3, 4, 5, 6, tzinfo=UTC)
"""@brief 确定性 adapter 测试时刻 / Deterministic adapter-test instant."""

SPACE = EmbeddingSpace(
    space_id="retrieval.recovery-test",
    model="test-model",
    dimensions=1024,
    query_instruction="Represent the query",
    passage_format_version=1,
)
"""@brief 与 PostgreSQL v1 schema 一致的测试 space / Test space matching the PostgreSQL v1 schema."""


def _processing_row(index: int) -> tuple[object, ...]:
    """@brief 创建一个已过期 processing row / Create one expired processing row.

    @param index 唯一 row 序号 / Unique row ordinal.
    @return 完整十三列 pre-state / Complete thirteen-column pre-state.
    """

    return (
        UUID(int=index + 1),
        SPACE.space_id,
        "processing",
        index + 1,
        1,
        None,
        UUID(int=10_000 + index),
        NOW - timedelta(seconds=1),
        None,
        None,
        NOW - timedelta(seconds=10),
        NOW - timedelta(seconds=2),
        None,
    )


def _recovered_row(row: tuple[object, ...]) -> tuple[object, ...]:
    """@brief 派生领域预期的 retry-wait post-state / Derive the domain-expected retry-wait post-state.

    @param row Processing pre-state / Processing pre-state.
    @return 完整十三列 post-state / Complete thirteen-column post-state.
    """

    return (
        row[0],
        row[1],
        "retry_wait",
        int(str(row[3])) + 1,
        row[4],
        NOW,
        None,
        None,
        None,
        RECOVERED_PASSAGE_VECTOR_LEASE_ERROR,
        row[10],
        NOW,
        None,
    )


def _passage(ordinal: int) -> RetrievalPassage:
    """@brief 创建稳定排序测试 Passage / Create a stable-order test passage.

    @param ordinal 来源内序号 / Source ordinal.
    @return 规范 Passage / Canonical passage.
    """

    return RetrievalPassage.create(
        corpus_id="conversation.episodic",
        scope=RetrievalScope("personal", 42),
        source_kind="conversation.turn",
        source_id=UUID("00000000-0000-0000-0000-000000000099"),
        ordinal=ordinal,
        format_version=SPACE.passage_format_version,
        text=f"User: passage {ordinal}",
        occurred_at=NOW - timedelta(minutes=1),
    )


def _passage_row(passage: RetrievalPassage) -> tuple[object, ...]:
    """@brief 把 Passage 编码为 adapter 读取顺序 / Encode a passage in adapter read order.

    @param passage 领域 Passage / Domain passage.
    @return 十一列数据库 row / Eleven-column database row.
    """

    return (
        passage.passage_id,
        passage.corpus_id,
        passage.scope.kind,
        passage.scope.scope_id,
        passage.source_kind,
        passage.source_id,
        passage.ordinal,
        passage.format_version,
        passage.text,
        passage.content_digest,
        passage.occurred_at,
    )


def test_claim_invokes_domain_before_batched_cas_and_preserves_ready_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Claim 先恢复领域 pre-state，再 CAS，并屏蔽 RETURNING 无序 / Claim restores domain pre-state before CAS and masks unordered RETURNING.

    @param monkeypatch Pytest 替换工具 / Pytest replacement helper.
    """

    passages = (_passage(1), _passage(0))
    candidate_rows = tuple(
        (
            passage.passage_id,
            SPACE.space_id,
            "pending",
            0,
            0,
            NOW,
            None,
            None,
            None,
            None,
            NOW,
            NOW,
            None,
            *_passage_row(passage),
        )
        for passage in passages
    )
    fake_connection = object()
    observed_sql: list[str] = []
    transaction_entries = 0

    @asynccontextmanager
    async def transaction() -> AsyncIterator[object]:
        """@brief 提供 claim fake transaction / Provide the claim fake transaction."""

        nonlocal transaction_entries
        transaction_entries += 1
        yield fake_connection

    async def fetch_all(
        sql: str,
        params: Iterable[object] | None = None,
        *,
        mapping: bool = False,
        connection: object | None = None,
    ) -> list[tuple[object, ...]]:
        """@brief 返回 candidates 或反序 RETURNING rows / Return candidates or reversed RETURNING rows.

        @param sql Adapter SQL / Adapter SQL.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否请求 mapping row / Whether mapping rows were requested.
        @param connection 当前事务连接 / Current transaction connection.
        @return 模拟 rows / Simulated rows.
        """

        assert mapping is False and connection is fake_connection
        observed_sql.append(sql)
        if sql.startswith("SELECT "):
            return list(candidate_rows)
        values = tuple(params or ())
        lease_expires_at = values[-3]
        updated_at = values[-2]
        post_rows = []
        for offset, passage in enumerate(passages):
            record = values[offset * 6 : offset * 6 + 6]
            post_rows.append(
                (
                    passage.passage_id,
                    SPACE.space_id,
                    "processing",
                    record[3],
                    record[4],
                    None,
                    UUID(str(record[5])),
                    lease_expires_at,
                    None,
                    None,
                    NOW,
                    updated_at,
                    None,
                )
            )
        return list(reversed(post_rows))

    monkeypatch.setattr(db, "transaction", transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)

    claims = asyncio.run(
        PostgresRetrievalStore().claim_vectors(
            space=SPACE,
            now=NOW,
            limit=2,
            lease_for=timedelta(seconds=30),
        )
    )

    assert transaction_entries == 1
    assert tuple(claim.passage for claim in claims) == passages
    assert all(claim.job.version == claim.job.attempt_count == 1 for claim in claims)
    select_sql, update_sql = observed_sql
    assert "FOR UPDATE OF vector SKIP LOCKED" in select_sql
    assert "ORDER BY vector.next_attempt_at, vector.passage_id" in select_sql
    assert "vector.version = decision.expected_version" in update_sql
    assert "RETURNING" in update_sql


def test_recovery_chunks_without_changing_full_count_or_transaction_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Recovery 在一个事务内遍历所有有界 chunk 且不使用 SKIP LOCKED / Recovery traverses every bounded chunk in one transaction without SKIP LOCKED.

    @param monkeypatch Pytest 替换工具 / Pytest replacement helper.
    """

    pre_rows = tuple(_processing_row(index) for index in range(130))
    post_rows = tuple(_recovered_row(row) for row in pre_rows)
    fake_connection = object()
    transaction_entries = 0
    transaction_exits = 0
    select_calls = 0
    update_calls = 0
    observed_sql: list[str] = []

    @asynccontextmanager
    async def transaction() -> AsyncIterator[object]:
        """@brief 提供唯一 fake transaction / Provide the sole fake transaction."""

        nonlocal transaction_entries, transaction_exits
        transaction_entries += 1
        try:
            yield fake_connection
        finally:
            transaction_exits += 1

    async def fetch_all(
        sql: str,
        params: Iterable[object] | None = None,
        *,
        mapping: bool = False,
        connection: object | None = None,
    ) -> list[tuple[object, ...]]:
        """@brief 按 SELECT/UPDATE 调用次序返回两个 chunk / Return two chunks according to SELECT/UPDATE call order.

        @param sql Adapter SQL / Adapter SQL.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否请求 mapping row / Whether mapping rows were requested.
        @param connection 当前事务连接 / Current transaction connection.
        @return 模拟数据库 rows / Simulated database rows.
        """

        del params
        nonlocal select_calls, update_calls
        assert mapping is False
        assert connection is fake_connection
        observed_sql.append(sql)
        if sql.startswith("SELECT "):
            start = select_calls * 128
            select_calls += 1
            return list(pre_rows[start : start + 128])
        start = update_calls * 128
        update_calls += 1
        return list(post_rows[start : start + 128])

    monkeypatch.setattr(db, "transaction", transaction)
    monkeypatch.setattr(db, "fetch_all", fetch_all)

    recovered = asyncio.run(
        PostgresRetrievalStore().recover_expired_vector_leases(space=SPACE, now=NOW)
    )

    assert recovered == 130
    assert transaction_entries == transaction_exits == 1
    assert select_calls == update_calls == 2
    selects = tuple(sql for sql in observed_sql if sql.startswith("SELECT "))
    assert all("SKIP LOCKED" not in sql for sql in selects)
    assert all(
        "ORDER BY vector.lease_expires_at, vector.passage_id" in sql for sql in selects
    )
    updates = tuple(sql for sql in observed_sql if sql.startswith("WITH decisions"))
    assert all("vector.version = decision.expected_version" in sql for sql in updates)
    assert all(
        "vector.claim_token = decision.expected_claim_token" in sql for sql in updates
    )


def test_completion_uses_committed_claim_cas_without_preread_and_accepts_float32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Completion 直接使用 sealed snapshot 做 version/token CAS，并按 float32 验证 / Completion directly CASes the sealed snapshot and validates at float32 precision.

    @param monkeypatch Pytest 替换工具 / Pytest replacement helper.
    """

    passage = _passage(0)
    claim = (
        PassageVectorJob.create_pending(
            PassageVectorJobKey(passage.passage_id, SPACE.space_id),
            created_at=NOW,
        )
        .claim(
            passage=passage,
            space=SPACE,
            claim_token=UUID("00000000-0000-0000-0000-000000000123"),
            claimed_at=NOW,
            lease_for=timedelta(seconds=30),
        )
        .claim
    )
    completed_at = NOW + timedelta(seconds=31)
    fake_connection = object()
    observed_sql: list[str] = []

    @asynccontextmanager
    async def transaction() -> AsyncIterator[object]:
        """@brief 提供 completion fake transaction / Provide the completion fake transaction."""

        yield fake_connection

    async def fetch_one(
        sql: str,
        params: Iterable[object] | None = None,
        *,
        mapping: bool = False,
        connection: object | None = None,
    ) -> tuple[object, ...]:
        """@brief 返回 pgvector 已量化的 completed row / Return a pgvector-quantized completed row.

        @param sql Adapter SQL / Adapter SQL.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否请求 mapping row / Whether mapping rows were requested.
        @param connection 当前事务连接 / Current transaction connection.
        @return 完整 completed row / Complete completed row.
        """

        assert mapping is False and connection is fake_connection
        values = tuple(params or ())
        observed_sql.append(sql)
        return (
            passage.passage_id,
            SPACE.space_id,
            "completed",
            values[0],
            claim.job.attempt_count,
            None,
            None,
            None,
            json.dumps((0.12345679104328156, *([0.0] * 1023))),
            None,
            claim.job.created_at,
            completed_at,
            completed_at,
        )

    monkeypatch.setattr(db, "transaction", transaction)
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    asyncio.run(
        PostgresRetrievalStore().complete_vector(
            claim,
            EmbeddingVector((0.123456789, *([0.0] * 1023))),
            completed_at=completed_at,
        )
    )

    assert len(observed_sql) == 1
    sql = observed_sql[0]
    assert sql.startswith("UPDATE retrieval.passage_vectors")
    assert "vector.version = %s" in sql
    assert "vector.claim_token = CAST(%s AS UUID)" in sql
    assert "RETURNING" in sql
