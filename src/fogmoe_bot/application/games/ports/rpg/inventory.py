"""@brief RPG 库存应用端口 / RPG inventory application port."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.games.rpg.inventory_models import (
    AddInventoryItem,
    InventoryResult,
    RemoveInventoryItem,
    UseItem,
)
from fogmoe_bot.domain.games import InventoryEntry, Item


class RpgInventoryOperations(Protocol):
    """@brief RPG 库存原子端口 / Atomic RPG inventory port.

    @note 容量、数量、消耗效果与回执处于同一短事务 /
    Capacity, quantity, consumption effects, and receipts share one short transaction.
    """

    async def inventory(self, user_id: int) -> tuple[InventoryEntry, ...]:
        """@brief 读取玩家背包 / Read player inventory."""

        ...

    async def item_details(self, item_id: int) -> Item | None:
        """@brief 读取道具定义 / Read item definition."""

        ...

    async def use_item(self, command: UseItem) -> InventoryResult:
        """@brief 幂等消耗一个道具 / Idempotently consume one item."""

        ...

    async def add_inventory_item(self, command: AddInventoryItem) -> InventoryResult:
        """@brief 幂等增加道具并执行容量约束 / Idempotently add an item under the capacity invariant."""

        ...

    async def remove_inventory_item(
        self, command: RemoveInventoryItem
    ) -> InventoryResult:
        """@brief 幂等移除指定数量道具 / Idempotently remove an item quantity."""

        ...
