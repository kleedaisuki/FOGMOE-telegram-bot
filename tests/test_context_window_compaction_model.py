"""@brief Context Window compaction 领域模型测试 / Context-window compaction domain-model tests."""

from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.domain.context_window.budget import ContextTokenBudget, TokenCount
from fogmoe_bot.domain.context_window.compaction import (
    Compaction,
    CompactionPlan,
    CompactionStatus,
    CompactionSummary,
    StaleCompactionClaimError,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    TurnId,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 确定性测试时钟 / Deterministic test clock."""


def _draft() -> CompactionPlan:
    """@brief 构造合法 compaction draft / Build a valid compaction draft.

    @return draft / Draft.
    """

    return CompactionPlan.create(
        conversation_id=ConversationId("assistant-user:7"),
        owner_user_id=7,
        epoch_floor_sequence=0,
        from_sequence=1,
        through_sequence=4,
        anchor_turn_id=TurnId.new(),
        predecessor_compaction_id=None,
        projection_version=1,
        source_snapshot=(
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ),
        source_row_count=4,
        source_token_count=TokenCount(12),
        created_at=NOW,
    )


def test_compaction_identity_and_digest_are_stable_and_source_drift_fails() -> None:
    """@brief 相同 range/snapshot 得到稳定 identity，snapshot 漂移立即失败 / Identical ranges and snapshots are stable, while snapshot drift fails fast."""

    first = _draft()
    second = CompactionPlan.create(
        conversation_id=first.conversation_id,
        owner_user_id=first.owner_user_id,
        epoch_floor_sequence=0,
        from_sequence=1,
        through_sequence=4,
        anchor_turn_id=first.anchor_turn_id,
        predecessor_compaction_id=None,
        projection_version=1,
        source_snapshot=first.source_snapshot,
        source_row_count=4,
        source_token_count=TokenCount(12),
        created_at=NOW,
    )
    assert first.compaction_id == second.compaction_id
    assert first.source_digest == second.source_digest

    with pytest.raises(ValueError, match="digest"):
        CompactionPlan(
            compaction_id=first.compaction_id,
            conversation_id=first.conversation_id,
            owner_user_id=first.owner_user_id,
            epoch_floor_sequence=first.epoch_floor_sequence,
            from_sequence=first.from_sequence,
            through_sequence=first.through_sequence,
            anchor_turn_id=first.anchor_turn_id,
            predecessor_compaction_id=None,
            projection_version=first.projection_version,
            source_digest="0" * 64,
            source_snapshot=first.source_snapshot,
            source_row_count=first.source_row_count,
            source_token_count=first.source_token_count,
            created_at=NOW,
        )

    with pytest.raises(ValueError, match="Out of range"):
        CompactionPlan.create(
            conversation_id=ConversationId("assistant-user:7"),
            owner_user_id=7,
            epoch_floor_sequence=0,
            from_sequence=1,
            through_sequence=1,
            anchor_turn_id=TurnId.new(),
            predecessor_compaction_id=None,
            projection_version=1,
            source_snapshot=({"role": "user", "content": float("nan")},),
            source_row_count=1,
            source_token_count=TokenCount(1),
            created_at=NOW,
        )


def test_claim_retry_reclaim_complete_and_stale_token_are_explicit() -> None:
    """@brief retry 后旧 token 失效，新 token 可唯一完成 / Retry invalidates the old token and only the new token can complete."""

    pending = Compaction.pending(_draft())
    old_token = LeaseToken.new()
    first_claim = pending.claim(
        token=old_token,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
    )
    retrying = first_claim.retry(
        token=old_token,
        failed_at=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(seconds=2),
        error="provider unavailable",
    )
    new_token = LeaseToken.new()
    second_claim = retrying.claim(
        token=new_token,
        claimed_at=NOW + timedelta(seconds=2),
        lease_for=timedelta(seconds=30),
    )
    summary = CompactionSummary("stable summary", TokenCount(3), "fake:model")

    with pytest.raises(StaleCompactionClaimError):
        second_claim.complete(
            token=old_token,
            summary=summary,
            completed_at=NOW + timedelta(seconds=3),
        )

    completed = second_claim.complete(
        token=new_token,
        summary=summary,
        completed_at=NOW + timedelta(seconds=3),
    )
    assert completed.status is CompactionStatus.COMPLETED
    assert completed.completion_token == new_token
    assert completed.attempt_count == 2


def test_expired_lease_recovers_without_reusing_token() -> None:
    """@brief 过期 lease 回到 retry_wait 并清除 token / Expired leases return to retry-wait and clear their token."""

    claim = Compaction.pending(_draft()).claim(
        token=LeaseToken.new(),
        claimed_at=NOW,
        lease_for=timedelta(seconds=1),
    )
    recovered = claim.recover_expired(now=NOW + timedelta(seconds=1))
    assert recovered.status is CompactionStatus.RETRY_WAIT
    assert recovered.claim_token is None
    assert recovered.next_attempt_at is not None


def test_token_budget_requires_strict_product_boundaries() -> None:
    """@brief token budget 不允许反转 warn/hard/summary 次序 / Token budgets reject reversed summary, warning, and hard ordering."""

    assert ContextTokenBudget().minimum_recent_non_tool_messages == 10
    with pytest.raises(ValueError, match="summary < warning < hard"):
        ContextTokenBudget(
            warning_tokens=TokenCount(100),
            hard_tokens=TokenCount(100),
            summary_output_tokens=TokenCount(10),
            segment_input_tokens=TokenCount(50),
        )
