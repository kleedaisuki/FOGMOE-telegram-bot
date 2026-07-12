"""@brief Games 纯领域规则契约 / Pure-domain contracts for Games."""

from __future__ import annotations

from datetime import UTC, date, datetime
import random
from uuid import UUID

import pytest

from fogmoe_bot.domain.games import (
    BattleResult,
    Character,
    Combatant,
    DiceRoll,
    FortuneLevel,
    GambleBet,
    GambleSession,
    GameSessionId,
    MONSTERS,
    SicBoBet,
    SicBoOutcome,
    daily_fortune,
    fight_monster,
    fight_players,
)


def _session() -> GambleSession:
    """@brief 创建稳定奖池 fixture / Build a stable pool fixture.

    @return 空活动奖池 / Empty active pool.
    """

    return GambleSession(
        GameSessionId(UUID("00000000-0000-0000-0000-000000000001")),
        -100,
        10,
        datetime(2030, 1, 1, tzinfo=UTC),
    )


def test_daily_fortune_is_deterministic_without_mutating_global_random_state() -> None:
    """@brief 每日签稳定且不污染共享 PRNG / Daily fortune is stable and does not mutate shared PRNG state."""

    random.seed(12345)
    first = random.random()
    fortune = daily_fortune(42, date(2026, 7, 11))
    second = random.random()

    random.seed(12345)
    assert first == random.random()
    assert second == random.random()
    assert fortune is daily_fortune(42, date(2026, 7, 11))
    assert isinstance(fortune, FortuneLevel)


def test_gamble_weights_cover_every_integer_ticket_and_reject_duplicate_player() -> (
    None
):
    """@brief 权重票完整覆盖且同玩家只能下注一次 / Weighted tickets cover the pool and a player may wager once."""

    now = datetime(2029, 1, 1, tzinfo=UTC)
    session = _session().place(GambleBet(1, "alice", 5), now=now)
    session = session.place(GambleBet(2, "bob", 10), now=now)

    winners: list[int] = []
    for ticket in range(session.prize):
        winner = session.winner_for_ticket(ticket)
        assert winner is not None
        winners.append(winner.user_id)
    assert set(winners[:5]) == {1}
    assert set(winners[5:]) == {2}
    with pytest.raises(ValueError, match="already joined"):
        session.place(GambleBet(1, "alice", 20), now=now)


def test_sicbo_preserves_triple_big_small_exception_and_gross_payouts() -> None:
    """@brief 围骰使大小输但单双照常且派奖含本金 / Triples lose big/small, retain parity, and payouts include stake."""

    roll = DiceRoll((4, 4, 4))

    assert not roll.wins(SicBoBet.BIG)
    assert not roll.wins(SicBoBet.SMALL)
    assert roll.wins(SicBoBet.EVEN)
    assert roll.wins(SicBoBet.ANY_TRIPLE)
    assert roll.wins(SicBoBet.TRIPLE_4)
    assert SicBoOutcome.resolve(SicBoBet.TRIPLE_4, 5, roll).credited == 905


def test_rpg_defender_attacks_first_and_level_up_applies_legacy_growth() -> None:
    """@brief PVP 被挑战者先攻且升级沿用旧成长 / PVP is defender-first and leveling retains legacy growth."""

    attacker = Character(1, hp=10, max_hp=10, attack=9, defense=1)
    defender = Character(2, hp=10, max_hp=10, attack=3, defense=1)
    battle = fight_players(Combatant("alice", attacker), Combatant("bob", defender))

    assert battle.turns[0].attacker == "bob"
    assert battle.winner_id == 1
    upgraded, event = attacker.gain_experience(100)
    assert upgraded.level == 2
    assert upgraded.max_hp == 15 and upgraded.hp == 15
    assert upgraded.attack == 10 and upgraded.defense == 2
    assert event is not None and event.new_level == 2


def test_zero_damage_monster_battle_terminates_as_draw_after_bounded_actions() -> None:
    """@brief 零伤害 PVE 在有界动作后平局 / Zero-damage PVE terminates as a draw after bounded actions."""

    player = Character(1, attack=0, defense=100)
    battle = fight_monster(Combatant("tank", player), MONSTERS["goblin"])

    assert battle.result is BattleResult.DRAW
    assert len(battle.turns) == 20
