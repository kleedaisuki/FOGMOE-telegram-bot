"""@brief RPG 库存命令与结果 / RPG inventory commands and results."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.domain.games import InventoryEntry, Item


@dataclass(frozen=True, slots=True)
class UseItem:
    """@brief 使用一个消耗品 / Use-one-consumable command."""

    user_id: int
    item_id: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AddInventoryItem:
    """@brief 向背包增加道具 / Add-inventory-item command."""

    user_id: int
    item_id: int
    quantity: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RemoveInventoryItem:
    """@brief 从背包移除道具 / Remove-inventory-item command."""

    user_id: int
    item_id: int
    quantity: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class InventoryResult:
    """@brief 背包用例结果 / Inventory use-case result."""

    code: RpgCode
    entries: tuple[InventoryEntry, ...] = ()
    item: Item | None = None
    replayed: bool = False
