"""@brief 版本化成员验证领域聚合测试 / Tests for the versioned member-verification aggregate."""

from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.domain.moderation.models import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    InvalidVerificationTransition,
    StaleVerificationVersion,
    VerificationEvent,
    VerificationKey,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
    hash_verification_token,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""

KEY = VerificationKey(ChatId(-1001), UserId(42))
"""@brief 固定聚合键 / Fixed aggregate key."""


def _creating(token: str = "secret") -> VerificationTask:
    """@brief 创建初始聚合 / Build an initial aggregate.

    @param token 明文 token / Plain token.
    @return CREATING 聚合 / CREATING aggregate.
    """

    return VerificationTask(
        key=KEY,
        version=VerificationVersion(0),
        token_hash=hash_verification_token(token),
        member_name="Alice",
        expires_at=NOW + timedelta(minutes=5),
    )


def _pending(token: str = "secret") -> VerificationTask:
    """@brief 创建 PENDING 聚合 / Build a PENDING aggregate.

    @param token 明文 token / Plain token.
    @return PENDING 聚合 / PENDING aggregate.
    """

    creating = _creating(token)
    return creating.evolve(
        VerificationEvent.ACTIVATE,
        expected_version=creating.version,
        now=NOW,
        message_id=MessageId(7),
    )


def test_creation_activation_binds_message_and_increments_version() -> None:
    """@brief ACTIVATE 将创建意图变为版本化 PENDING / ACTIVATE turns creation intent into versioned PENDING."""

    pending = _pending()

    assert pending.status is VerificationStatus.PENDING
    assert pending.version == VerificationVersion(1)
    assert pending.message_id == MessageId(7)
    assert pending.key == KEY


def test_pending_accepts_only_matching_unexpired_token() -> None:
    """@brief PENDING 仅接受正确且未过期 token / PENDING accepts only a matching unexpired token."""

    task = _pending()

    assert task.accepts("secret", NOW + timedelta(seconds=1)) is True
    assert task.accepts("wrong", NOW + timedelta(seconds=1)) is False
    assert task.accepts("secret", task.expires_at) is False


def test_pass_and_timeout_are_mutually_exclusive_versioned_transitions() -> None:
    """@brief PASS 与 TIMEOUT 由同一版本竞争且只能一个获胜 / PASS and TIMEOUT compete on one version and only one can win."""

    pending = _pending()
    passing = pending.evolve(
        VerificationEvent.PASS_REQUESTED,
        expected_version=pending.version,
        now=NOW + timedelta(minutes=1),
    )

    assert passing.status is VerificationStatus.PASSING
    with pytest.raises(StaleVerificationVersion):
        passing.evolve(
            VerificationEvent.DEADLINE_REACHED,
            expected_version=pending.version,
            now=pending.expires_at,
        )
    with pytest.raises(InvalidVerificationTransition):
        passing.evolve(
            VerificationEvent.MEMBER_LEFT,
            expected_version=passing.version,
            now=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("transitional", "terminal"),
    [
        (VerificationStatus.PASSING, VerificationStatus.PASSED),
        (VerificationStatus.EXPIRING, VerificationStatus.EXPIRED),
        (VerificationStatus.CANCELLING, VerificationStatus.CANCELLED),
    ],
)
def test_effect_acknowledgement_maps_each_transition_to_one_terminal_state(
    transitional: VerificationStatus,
    terminal: VerificationStatus,
) -> None:
    """@brief EFFECT_DELIVERED 将每个过渡态映射到唯一终态 / EFFECT_DELIVERED maps each transitional state to one terminal state.

    @param transitional 过渡态 / Transitional state.
    @param terminal 期望终态 / Expected terminal state.
    @return None / None.
    """

    pending = _pending()
    event = {
        VerificationStatus.PASSING: VerificationEvent.PASS_REQUESTED,
        VerificationStatus.EXPIRING: VerificationEvent.DEADLINE_REACHED,
        VerificationStatus.CANCELLING: VerificationEvent.MEMBER_LEFT,
    }[transitional]
    event_now = (
        pending.expires_at if event is VerificationEvent.DEADLINE_REACHED else NOW
    )
    transition = pending.evolve(
        event,
        expected_version=pending.version,
        now=event_now,
    )
    finished = transition.evolve(
        VerificationEvent.EFFECT_DELIVERED,
        expected_version=transition.version,
        now=event_now,
    )

    assert finished.status is terminal
    assert finished.version == transition.version.next()
    assert finished.status.terminal is True


def test_abandoned_creation_can_be_cancelled_without_a_message_id() -> None:
    """@brief CREATING crash recovery 可在没有消息 ID 时完成补偿 / CREATING crash recovery can compensate without a message ID."""

    creating = _creating()
    cancelling = creating.evolve(
        VerificationEvent.ABORT_CREATION,
        expected_version=creating.version,
        now=NOW,
    )
    cancelled = cancelling.evolve(
        VerificationEvent.EFFECT_DELIVERED,
        expected_version=cancelling.version,
        now=NOW,
    )

    assert cancelled.status is VerificationStatus.CANCELLED
    assert cancelled.message_id is None
