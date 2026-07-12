"""@brief 猜拳纯领域聚合测试 / Tests for the pure RPS domain aggregate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.domain.games import (
    Choice,
    GameCancellation,
    GameId,
    GameSession,
    GameStatus,
    GameVersion,
    OutcomeKind,
    Player,
    RpsDomainError,
    StaleGameVersion,
    UserId,
    WaitingRoom,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
"""@brief 测试使用的稳定 UTC 时刻 / Stable UTC instant used by tests."""

FIRST = Player(UserId(101), "alice")
"""@brief 第一位测试玩家 / First test player."""

SECOND = Player(UserId(202), "bob")
"""@brief 第二位测试玩家 / Second test player."""

GAME_ID = GameId("game_abcd")
"""@brief 稳定测试游戏身份 / Stable test game identity."""


def _session() -> GameSession:
    """@brief 创建初始选择阶段会话 / Build an initial choosing session.

    @return 版本一的活动会话 / Active version-one session.
    """

    room = WaitingRoom.open(GAME_ID, FIRST, now=NOW, wait_for=timedelta(minutes=10))
    return room.join(
        SECOND,
        expected_version=room.version,
        now=NOW + timedelta(seconds=1),
        choose_for=timedelta(minutes=2),
    )


def test_identity_value_objects_reject_ambiguous_or_callback_unsafe_values() -> None:
    """@brief 身份值对象拒绝 bool、非正用户和不安全游戏 ID / Identity values reject bools, non-positive users, and unsafe IDs."""

    with pytest.raises(TypeError):
        UserId(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        UserId(0)
    with pytest.raises(ValueError):
        GameId("too short")
    assert int(UserId(42)) == 42
    assert str(GameId("abcdefgh")) == "abcdefgh"


def test_waiting_room_enforces_version_expiry_and_distinct_players() -> None:
    """@brief 等待房间在纯转移中约束版本、截止时间和玩家唯一性 / Waiting transitions enforce version, deadline, and distinct players."""

    room = WaitingRoom.open(GAME_ID, FIRST, now=NOW, wait_for=timedelta(minutes=1))

    with pytest.raises(StaleGameVersion):
        room.join(
            SECOND,
            expected_version=GameVersion(1),
            now=NOW,
            choose_for=timedelta(minutes=1),
        )
    with pytest.raises(RpsDomainError, match="host cannot join"):
        room.join(
            FIRST,
            expected_version=room.version,
            now=NOW,
            choose_for=timedelta(minutes=1),
        )
    with pytest.raises(RpsDomainError, match="expired"):
        room.join(
            SECOND,
            expected_version=room.version,
            now=room.expires_at,
            choose_for=timedelta(minutes=1),
        )

    session = room.join(
        SECOND,
        expected_version=room.version,
        now=NOW,
        choose_for=timedelta(minutes=1),
    )
    assert session.game_id == room.game_id
    assert session.version == GameVersion(1)
    assert session.status is GameStatus.CHOOSING


@pytest.mark.parametrize(
    ("first_choice", "second_choice", "kind", "winner"),
    [
        (Choice.ROCK, Choice.ROCK, OutcomeKind.DRAW, None),
        (Choice.PAPER, Choice.PAPER, OutcomeKind.DRAW, None),
        (Choice.SCISSORS, Choice.SCISSORS, OutcomeKind.DRAW, None),
        (Choice.ROCK, Choice.SCISSORS, OutcomeKind.PLAYER_ONE, FIRST.user_id),
        (Choice.PAPER, Choice.ROCK, OutcomeKind.PLAYER_ONE, FIRST.user_id),
        (Choice.SCISSORS, Choice.PAPER, OutcomeKind.PLAYER_ONE, FIRST.user_id),
        (Choice.SCISSORS, Choice.ROCK, OutcomeKind.PLAYER_TWO, SECOND.user_id),
        (Choice.ROCK, Choice.PAPER, OutcomeKind.PLAYER_TWO, SECOND.user_id),
        (Choice.PAPER, Choice.SCISSORS, OutcomeKind.PLAYER_TWO, SECOND.user_id),
    ],
)
def test_all_choice_pairs_finish_immediately_and_conserve_the_coin_pot(
    first_choice: Choice,
    second_choice: Choice,
    kind: OutcomeKind,
    winner: UserId | None,
) -> None:
    """@brief 九种手势组合均立即完成且金币池守恒 / All nine choice pairs finish immediately and conserve the pot.

    @param first_choice 第一位玩家选择 / First-player choice.
    @param second_choice 第二位玩家选择 / Second-player choice.
    @param kind 期望结果类别 / Expected outcome kind.
    @param winner 期望赢家 / Expected winner.
    @return None / None.
    """

    session = _session()
    after_first = session.choose(
        FIRST.user_id,
        first_choice,
        expected_version=session.version,
        now=NOW + timedelta(seconds=2),
    )
    finished = after_first.choose(
        SECOND.user_id,
        second_choice,
        expected_version=after_first.version,
        now=NOW + timedelta(seconds=3),
    )

    assert finished.status is GameStatus.FINISHED
    assert finished.version == GameVersion(3)
    assert finished.outcome is not None
    assert finished.outcome.kind is kind
    assert finished.outcome.winner == winner
    assert sum(payout.coins for payout in finished.outcome.payouts) == 2


def test_old_choice_version_is_rejected_without_mutating_the_session() -> None:
    """@brief 旧按钮版本被拒绝且不可变聚合保持原值 / A stale button version is rejected without mutating the immutable aggregate."""

    session = _session()
    updated = session.choose(
        FIRST.user_id,
        Choice.ROCK,
        expected_version=session.version,
        now=NOW + timedelta(seconds=2),
    )

    with pytest.raises(StaleGameVersion):
        updated.choose(
            SECOND.user_id,
            Choice.PAPER,
            expected_version=session.version,
            now=NOW + timedelta(seconds=3),
        )
    assert session.player_one_choice is None
    assert updated.player_one_choice is Choice.ROCK
    assert updated.player_two_choice is None


def test_cancellation_is_terminal_versioned_and_refunds_both_players() -> None:
    """@brief 取消产生终态新版本并显式生成双方退款 / Cancellation produces a versioned terminal state and explicit refunds."""

    session = _session()
    cancelled = session.cancel(
        GameCancellation.TIMEOUT,
        expected_version=session.version,
    )

    assert cancelled.status is GameStatus.CANCELLED
    assert cancelled.version == session.version.next()
    assert cancelled.cancellation is GameCancellation.TIMEOUT
    assert {payout.user_id: payout.coins for payout in cancelled.refunds} == {
        FIRST.user_id: 1,
        SECOND.user_id: 1,
    }
    with pytest.raises(RpsDomainError):
        cancelled.cancel(
            GameCancellation.SERVICE_SHUTDOWN,
            expected_version=cancelled.version,
        )
