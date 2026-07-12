"""@brief RPG 角色与战斗命令和结果 / RPG character and battle commands and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.domain.games import (
    Character,
    LevelUp,
    Monster,
    MonsterBattle,
    PlayerBattle,
)


@dataclass(frozen=True, slots=True)
class RpgProfile:
    """@brief RPG 角色状态结果 / RPG character-status result."""

    code: RpgCode
    character: Character | None = None
    balance: int | None = None
    created: bool = False


@dataclass(frozen=True, slots=True)
class SetBattleAllowance:
    """@brief 设置被挑战开关 / Set-challenge-allowance command."""

    user_id: int
    allow: bool
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class HealCharacter:
    """@brief 付费治疗命令 / Paid-heal command."""

    user_id: int
    cost: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RpgMutationResult:
    """@brief RPG 简单变更结果 / RPG simple-mutation result."""

    code: RpgCode
    character: Character | None = None
    balance: int | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FightMonster:
    """@brief 挑战怪物命令 / Fight-monster command."""

    user_id: int
    display_name: str
    monster_id: str
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MonsterBattleResult:
    """@brief 已提交 PVE 结果 / Committed PVE result."""

    code: RpgCode
    monster: Monster | None = None
    battle: MonsterBattle | None = None
    character: Character | None = None
    balance: int | None = None
    experience_reward: int = 0
    coin_reward: int = 0
    level_up: LevelUp | None = None
    cooldown_remaining: timedelta | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FightPlayer:
    """@brief 挑战玩家命令 / Fight-player command."""

    attacker_id: int
    attacker_name: str
    target_username: str
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PlayerBattleResult:
    """@brief 已提交 PVP 结果 / Committed PVP result."""

    code: RpgCode
    attacker_name: str | None = None
    defender_name: str | None = None
    battle: PlayerBattle | None = None
    winner_name: str | None = None
    loser_name: str | None = None
    coins_lost: int = 0
    coins_awarded: int = 0
    experience_awarded: int = 0
    level_up: LevelUp | None = None
    cooldown_remaining: timedelta | None = None
    replayed: bool = False
