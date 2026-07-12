"""@brief RPG 库存应用服务 / RPG inventory application service."""

from __future__ import annotations

from fogmoe_bot.application.games.ports.rpg.inventory import RpgInventoryOperations
from fogmoe_bot.application.games.rpg.inventory_models import (
    AddInventoryItem,
    InventoryResult,
    RemoveInventoryItem,
    UseItem,
)
from fogmoe_bot.domain.games import InventoryEntry, Item

RPG_INVENTORY_SERVICE_DATA_KEY = "games.rpg.inventory.service"
"""@brief runtime capability 中 RPG 库存服务的键 / RPG inventory-service capability key."""


class RpgInventoryService:
    """@brief 编排 RPG 库存读写用例 / Orchestrate RPG inventory use cases."""

    def __init__(self, operations: RpgInventoryOperations) -> None:
        self._operations = operations

    async def inventory(self, user_id: int) -> tuple[InventoryEntry, ...]:
        """@brief 读取背包 / Read an inventory."""

        return await self._operations.inventory(user_id)

    async def details(self, item_id: int) -> Item | None:
        """@brief 读取道具定义 / Read an item definition."""

        return await self._operations.item_details(item_id)

    async def use(self, command: UseItem) -> InventoryResult:
        """@brief 使用消耗品 / Use a consumable."""

        return await self._operations.use_item(command)

    async def add(self, command: AddInventoryItem) -> InventoryResult:
        """@brief 增加背包道具 / Add an inventory item."""

        if command.quantity <= 0:
            raise ValueError("Inventory addition quantity must be positive")
        return await self._operations.add_inventory_item(command)

    async def remove(self, command: RemoveInventoryItem) -> InventoryResult:
        """@brief 移除背包道具 / Remove an inventory item."""

        if command.quantity <= 0:
            raise ValueError("Inventory removal quantity must be positive")
        return await self._operations.remove_inventory_item(command)
