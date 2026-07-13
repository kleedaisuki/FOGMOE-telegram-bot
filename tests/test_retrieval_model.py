"""@brief Retrieval 领域与情景 renderer 测试 / Tests for retrieval domain values and episodic rendering."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fogmoe_bot.application.retrieval import EpisodicPassageRenderer, EpisodicTurn
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalPassage,
    RetrievalScope,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


def _space() -> EmbeddingSpace:
    """@brief 构造测试空间 / Build a test embedding space."""

    return EmbeddingSpace(
        space_id="qwen.test.1024.v1",
        model="qwen/qwen3-embedding-8b",
        dimensions=1024,
        query_instruction="Retrieve relevant prior conversation evidence.",
        passage_format_version=1,
    )


def test_embedding_space_and_vectors_reject_semantic_drift() -> None:
    """@brief 空间维度、向量有限性与非零不变量均被类型守卫 / Types guard dimensions, finiteness, and non-zero invariants."""

    space = _space()
    vector = EmbeddingVector((1.0, *([0.0] * 1023)))
    vector.require_space(space)
    with pytest.raises(ValueError, match="dimension mismatch"):
        EmbeddingVector((1.0,)).require_space(space)
    with pytest.raises(ValueError, match="cannot be zero"):
        EmbeddingVector((0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        EmbeddingVector((float("nan"),))
    with pytest.raises(ValueError, match="between 1 and 2000"):
        EmbeddingSpace("bad", "model", 4096, "instruction", 1)


def test_passage_identity_is_deterministic_and_content_is_hashed() -> None:
    """@brief 来源 identity 决定 UUID，文本决定 digest / Source identity determines UUID while text determines the digest."""

    source_id = UUID("00000000-0000-0000-0000-000000000007")
    first = RetrievalPassage.create(
        corpus_id="conversation.episodic",
        scope=RetrievalScope("personal", 7),
        source_kind="conversation.turn",
        source_id=source_id,
        ordinal=0,
        format_version=1,
        text="User: tea",
        occurred_at=NOW,
    )
    replay = RetrievalPassage.create(
        corpus_id="conversation.episodic",
        scope=RetrievalScope("personal", 7),
        source_kind="conversation.turn",
        source_id=source_id,
        ordinal=0,
        format_version=1,
        text="User: tea",
        occurred_at=NOW,
    )
    assert first == replay
    with pytest.raises(ValueError, match="digest"):
        RetrievalPassage(
            passage_id=first.passage_id,
            corpus_id=first.corpus_id,
            scope=first.scope,
            source_kind=first.source_kind,
            source_id=first.source_id,
            ordinal=first.ordinal,
            format_version=first.format_version,
            text="different",
            content_digest=first.content_digest,
            occurred_at=first.occurred_at,
        )


def test_renderer_uses_natural_turn_boundary_and_hard_bounds_long_text() -> None:
    """@brief Renderer 保留角色/时间并对异常长 Turn 硬切分 / Renderer retains roles/time and hard-bounds oversized turns."""

    renderer = EpisodicPassageRenderer(max_characters=500)
    turn = EpisodicTurn(
        turn_id=UUID("00000000-0000-0000-0000-000000000008"),
        scope=RetrievalScope("personal", 8),
        user_text="我喜欢红茶。" * 80,
        assistant_text="记住了。" * 80,
        occurred_at=NOW,
    )
    passages = renderer.render(turn)
    assert len(passages) > 1
    assert all(len(passage.text) <= 500 for passage in passages)
    assert passages[0].text.startswith("Time: 2030-01-01T00:00:00Z\nUser:")
    assert [passage.ordinal for passage in passages] == list(range(len(passages)))
    assert all(passage.source_id == turn.turn_id for passage in passages)


def test_passage_identity_includes_the_privacy_scope() -> None:
    """@brief 相同来源在个人与不同群域中绝不共享 identity / The same source never shares identity across personal or group scopes."""

    source_id = UUID("00000000-0000-0000-0000-000000000099")
    passages = tuple(
        RetrievalPassage.create(
            corpus_id="conversation.episodic",
            scope=scope,
            source_kind="conversation.turn",
            source_id=source_id,
            ordinal=0,
            format_version=1,
            text="same content",
            occurred_at=NOW,
        )
        for scope in (
            RetrievalScope("personal", 7),
            RetrievalScope("group", -1001),
            RetrievalScope("group", -1002),
        )
    )
    assert len({passage.passage_id for passage in passages}) == 3
