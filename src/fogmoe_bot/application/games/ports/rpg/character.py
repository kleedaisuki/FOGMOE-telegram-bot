"""@brief RPG 角色与战斗应用端口 / RPG character and battle application port."""

from __future__ import annotations

from typing import Protocol

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


class RpgCharacterOperations(Protocol):
    """@brief RPG 角色与战斗原子端口 / Atomic RPG character and battle port.

    @note 账户、冷却与角色按稳定顺序锁定，状态和回执处于同一事务 /
    Accounts, cooldowns, and characters are locked in stable order with state and receipts in one transaction.
    """

    async def ensure_rpg_profile(self, user_id: int) -> RpgProfile:
        """@brief 读取或创建 RPG 角色 / Read or create an RPG character."""

        ...

    async def rpg_profile(self, user_id: int) -> RpgProfile:
        """@brief 只读 RPG 角色 / Read an RPG character without creating one."""

        ...

    async def set_battle_allowance(
        self, command: SetBattleAllowance
    ) -> RpgMutationResult:
        """@brief 幂等设置被挑战开关 / Idempotently set challenge allowance."""

        ...

    async def heal_character(self, command: HealCharacter) -> RpgMutationResult:
        """@brief 原子扣费并治疗 / Atomically charge and heal."""

        ...

    async def fight_monster(self, command: FightMonster) -> MonsterBattleResult:
        """@brief 原子执行 PVE 与冷却 / Atomically execute PVE and cooldown."""

        ...

    async def fight_player(self, command: FightPlayer) -> PlayerBattleResult:
        """@brief 按稳定身份锁序原子执行 PVP / Atomically execute PVP under stable identity lock order."""

        ...
