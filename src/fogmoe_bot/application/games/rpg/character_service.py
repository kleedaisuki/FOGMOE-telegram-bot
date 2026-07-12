"""@brief RPG 角色与战斗应用服务 / RPG character and battle application service."""

from __future__ import annotations

from typing import Final

from fogmoe_bot.application.games.ports.rpg.character import RpgCharacterOperations
from fogmoe_bot.application.games.rpg.character_models import (
    FightMonster,
    FightPlayer,
    HealCharacter,
    MonsterBattleResult,
    PlayerBattleResult,
    RpgMutationResult,
    RpgProfile,
    SetBattleAllowance,
)
from fogmoe_bot.domain.games import MONSTERS, Monster

RPG_CHARACTER_SERVICE_DATA_KEY = "games.rpg.character.service"
"""@brief runtime capability 中 RPG 角色服务的键 / RPG character-service capability key."""

HEAL_COST: Final = 10
"""@brief RPG 满血治疗旧价格 / Legacy RPG full-heal price."""


class RpgCharacterService:
    """@brief 编排角色、治疗与战斗用例 / Orchestrate character, healing, and battle use cases."""

    def __init__(self, operations: RpgCharacterOperations) -> None:
        self._operations = operations

    async def ensure_profile(self, user_id: int) -> RpgProfile:
        """@brief 读取或创建角色 / Read or create a character."""

        return await self._operations.ensure_rpg_profile(user_id)

    async def profile(self, user_id: int) -> RpgProfile:
        """@brief 只读角色状态 / Read character state without creating it."""

        return await self._operations.rpg_profile(user_id)

    async def set_battle_allowance(
        self, command: SetBattleAllowance
    ) -> RpgMutationResult:
        """@brief 设置被挑战开关 / Set challenge allowance."""

        return await self._operations.set_battle_allowance(command)

    async def heal(self, user_id: int, *, idempotency_key: str) -> RpgMutationResult:
        """@brief 按旧十金币价格治疗 / Heal for the legacy ten-coin price."""

        return await self._operations.heal_character(
            HealCharacter(user_id, HEAL_COST, idempotency_key)
        )

    async def fight_monster(self, command: FightMonster) -> MonsterBattleResult:
        """@brief 挑战怪物 / Fight a monster."""

        return await self._operations.fight_monster(command)

    async def fight_player(self, command: FightPlayer) -> PlayerBattleResult:
        """@brief 挑战玩家 / Fight a player."""

        return await self._operations.fight_player(command)

    @property
    def monsters(self) -> tuple[Monster, ...]:
        return tuple(MONSTERS.values())
