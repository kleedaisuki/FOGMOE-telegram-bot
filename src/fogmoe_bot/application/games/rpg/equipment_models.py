"""@brief RPG 装备命令与结果 / RPG equipment commands and results."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.domain.games import Equipment, EquipmentLoadout, EquipmentSlot


@dataclass(frozen=True, slots=True)
class EquipItem:
    """@brief 装备物品命令 / Equip-item command."""

    user_id: int
    equipment_id: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class UnequipItem:
    """@brief 卸下装备命令 / Unequip-item command."""

    user_id: int
    slot: EquipmentSlot
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class EquipmentResult:
    """@brief 装备用例结果 / Equipment use-case result."""

    code: RpgCode
    loadout: EquipmentLoadout | None = None
    equipment: Equipment | None = None
    replayed: bool = False
