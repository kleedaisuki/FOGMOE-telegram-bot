"""@brief RPG 装备应用端口 / RPG equipment application port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fogmoe_bot.application.games.rpg.equipment_models import (
    EquipItem,
    EquipmentResult,
    UnequipItem,
)
from fogmoe_bot.domain.games import Equipment, EquipmentLoadout

RPG_EQUIPMENT_OPERATIONS_DATA_KEY = "games.rpg.equipment.operations"
"""@brief runtime capability 中 RPG 装备端口的键 / RPG equipment-operations capability key."""


@runtime_checkable
class RpgEquipmentOperations(Protocol):
    """@brief RPG 装备原子端口 / Atomic RPG equipment port.

    @note 装备槽位、派生属性与回执处于同一短事务 /
    Equipment slots, derived statistics, and receipts share one short transaction.
    """

    async def equipment(self, user_id: int) -> EquipmentLoadout | None:
        """@brief 读取玩家装备 / Read player equipment."""

        ...

    async def equipment_details(self, equipment_id: int) -> Equipment | None:
        """@brief 读取装备定义 / Read equipment definition."""

        ...

    async def equip(self, command: EquipItem) -> EquipmentResult:
        """@brief 幂等装备物品 / Idempotently equip an item."""

        ...

    async def unequip(self, command: UnequipItem) -> EquipmentResult:
        """@brief 幂等卸下装备 / Idempotently unequip an item."""

        ...
