"""@brief RPG 库存 PostgreSQL adapter / PostgreSQL adapter for RPG inventory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.rpg.inventory_models import (
    AddInventoryItem,
    InventoryResult,
    RemoveInventoryItem,
    UseItem,
)
from fogmoe_bot.application.games.ports.rpg.inventory import RpgInventoryOperations
from fogmoe_bot.domain.games import (
    InventoryEntry,
    INVENTORY_CAPACITY,
    Item,
    ItemType,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from ..common import (
    _integer,
    _json_object,
    _load_receipt,
    _lock_receipt_key,
    _read_account,
    _save_receipt,
)
from .common import (
    _load_character,
)


class PostgresRpgInventoryOperations(RpgInventoryOperations):
    """@brief RPG 库存容量、数量、消耗效果与回执 adapter / RPG inventory adapter for capacity, quantity, effects, and receipts."""

    async def inventory(self, user_id: int) -> tuple[InventoryEntry, ...]:
        """@brief 读取背包 / Read inventory.

        @param user_id 玩家 ID / Player ID.
        @return 背包条目 / Inventory entries.
        """

        return await _load_inventory(user_id, None)

    async def item_details(self, item_id: int) -> Item | None:
        """@brief 读取道具定义 / Read an item definition.

        @param item_id 道具 ID / Item ID.
        @return 道具或 None / Item or None.
        """

        return await _load_item(item_id, None)

    async def use_item(self, command: UseItem) -> InventoryResult:
        """@brief 幂等消耗一个旧消耗品 / Idempotently consume one legacy consumable.

        @param command 使用命令 / Use command.
        @return 背包结果 / Inventory result.
        @note 旧 ``effect`` 仍是自由文本，故当前语义仅消耗数量 / Legacy ``effect`` remains free text, so current semantics only consume quantity.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.use_item",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _inventory_result_from_json(replay, replayed=True)
            character = await _load_character(command.user_id, connection)
            if character is None:
                result = InventoryResult(RpgCode.NO_CHARACTER)
            else:
                await _lock_inventory_gate(command.user_id, connection)
                row = await db_connection.fetch_one(
                    "SELECT inventory.quantity, inventory.version, items.id, items.name, "
                    "items.type, items.effect, items.description, items.price, items.use_limit "
                    "FROM game.rpg_player_inventory AS inventory "
                    "JOIN game.rpg_items AS items ON items.id = inventory.item_id "
                    "WHERE inventory.user_id = %s AND inventory.item_id = %s FOR UPDATE OF inventory",
                    (command.user_id, command.item_id),
                    connection=connection,
                )
                if row is None:
                    item = await _load_item(command.item_id, connection)
                    result = InventoryResult(
                        RpgCode.NOT_OWNED if item is not None else RpgCode.NOT_FOUND,
                        item=item,
                    )
                else:
                    item = _map_item(row[2:9])
                    quantity = int(row[0])
                    version = int(row[1])
                    if item.item_type is not ItemType.CONSUMABLE:
                        result = InventoryResult(RpgCode.WRONG_ITEM_TYPE, item=item)
                    else:
                        if quantity == 1:
                            affected = await db_connection.execute(
                                "DELETE FROM game.rpg_player_inventory WHERE user_id = %s "
                                "AND item_id = %s AND version = %s",
                                (command.user_id, command.item_id, version),
                                connection=connection,
                            )
                        else:
                            affected = await db_connection.execute(
                                "UPDATE game.rpg_player_inventory SET quantity = quantity - 1, "
                                "version = version + 1 WHERE user_id = %s AND item_id = %s "
                                "AND version = %s AND quantity > 1",
                                (command.user_id, command.item_id, version),
                                connection=connection,
                            )
                        if affected != 1:
                            raise RuntimeError(
                                "Inventory OCC update lost its locked row"
                            )
                        entries = await _load_inventory(command.user_id, connection)
                        result = InventoryResult(RpgCode.SUCCESS, entries, item)
            await _save_receipt(
                command.idempotency_key,
                "rpg.use_item",
                command.user_id,
                _inventory_result_to_json(result),
                connection,
            )
            return result

    async def add_inventory_item(self, command: AddInventoryItem) -> InventoryResult:
        """@brief 幂等增加道具且线性化容量检查 / Idempotently add an item and linearize the capacity check.

        @param command 增加命令 / Add command.
        @return 背包结果 / Inventory result.
        """

        if command.quantity <= 0:
            raise ValueError("Inventory addition quantity must be positive")
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.add_item",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _inventory_result_from_json(replay, replayed=True)
            account = await _read_account(command.user_id, connection)
            if account is None:
                result = InventoryResult(RpgCode.NOT_REGISTERED)
            else:
                await _lock_inventory_gate(command.user_id, connection)
                item = await _load_item(command.item_id, connection)
                if item is None:
                    result = InventoryResult(RpgCode.NOT_FOUND)
                else:
                    row = await db_connection.fetch_one(
                        "SELECT quantity, version FROM game.rpg_player_inventory "
                        "WHERE user_id = %s AND item_id = %s FOR UPDATE",
                        (command.user_id, command.item_id),
                        connection=connection,
                    )
                    if row is not None:
                        affected = await db_connection.execute(
                            "UPDATE game.rpg_player_inventory SET quantity = quantity + %s, "
                            "version = version + 1 WHERE user_id = %s AND item_id = %s "
                            "AND version = %s",
                            (
                                command.quantity,
                                command.user_id,
                                command.item_id,
                                _integer(row[1]),
                            ),
                            connection=connection,
                        )
                        if affected != 1:
                            raise RuntimeError(
                                "Inventory addition OCC lost its locked row"
                            )
                        result = InventoryResult(
                            RpgCode.SUCCESS,
                            await _load_inventory(command.user_id, connection),
                            item,
                        )
                    else:
                        count_row = await db_connection.fetch_one(
                            "SELECT COUNT(*) FROM game.rpg_player_inventory "
                            "WHERE user_id = %s",
                            (command.user_id,),
                            connection=connection,
                        )
                        count = _integer(count_row[0]) if count_row is not None else 0
                        if count >= INVENTORY_CAPACITY:
                            result = InventoryResult(
                                RpgCode.INVENTORY_FULL,
                                await _load_inventory(command.user_id, connection),
                                item,
                            )
                        else:
                            await db_connection.execute(
                                "INSERT INTO game.rpg_player_inventory "
                                "(user_id, item_id, quantity, version) VALUES (%s, %s, %s, 0)",
                                (command.user_id, command.item_id, command.quantity),
                                connection=connection,
                            )
                            result = InventoryResult(
                                RpgCode.SUCCESS,
                                await _load_inventory(command.user_id, connection),
                                item,
                            )
            await _save_receipt(
                command.idempotency_key,
                "rpg.add_item",
                command.user_id,
                _inventory_result_to_json(result),
                connection,
            )
            return result

    async def remove_inventory_item(
        self, command: RemoveInventoryItem
    ) -> InventoryResult:
        """@brief 幂等移除数量并保持正数量不变量 / Idempotently remove quantity while preserving the positive-quantity invariant.

        @param command 移除命令 / Remove command.
        @return 背包结果 / Inventory result.
        """

        if command.quantity <= 0:
            raise ValueError("Inventory removal quantity must be positive")
        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.remove_item",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _inventory_result_from_json(replay, replayed=True)
            await _lock_inventory_gate(command.user_id, connection)
            row = await db_connection.fetch_one(
                "SELECT inventory.quantity, inventory.version, items.id, items.name, "
                "items.type, items.effect, items.description, items.price, items.use_limit "
                "FROM game.rpg_player_inventory AS inventory "
                "JOIN game.rpg_items AS items ON items.id = inventory.item_id "
                "WHERE inventory.user_id = %s AND inventory.item_id = %s FOR UPDATE OF inventory",
                (command.user_id, command.item_id),
                connection=connection,
            )
            if row is None:
                item = await _load_item(command.item_id, connection)
                result = InventoryResult(
                    RpgCode.NOT_OWNED if item is not None else RpgCode.NOT_FOUND,
                    item=item,
                )
            else:
                quantity = _integer(row[0])
                version = _integer(row[1])
                item = _map_item(row[2:9])
                if quantity < command.quantity:
                    result = InventoryResult(
                        RpgCode.INSUFFICIENT_QUANTITY,
                        await _load_inventory(command.user_id, connection),
                        item,
                    )
                else:
                    if quantity == command.quantity:
                        affected = await db_connection.execute(
                            "DELETE FROM game.rpg_player_inventory WHERE user_id = %s "
                            "AND item_id = %s AND version = %s",
                            (command.user_id, command.item_id, version),
                            connection=connection,
                        )
                    else:
                        affected = await db_connection.execute(
                            "UPDATE game.rpg_player_inventory SET quantity = quantity - %s, "
                            "version = version + 1 WHERE user_id = %s AND item_id = %s "
                            "AND version = %s AND quantity > %s",
                            (
                                command.quantity,
                                command.user_id,
                                command.item_id,
                                version,
                                command.quantity,
                            ),
                            connection=connection,
                        )
                    if affected != 1:
                        raise RuntimeError("Inventory removal OCC lost its locked row")
                    result = InventoryResult(
                        RpgCode.SUCCESS,
                        await _load_inventory(command.user_id, connection),
                        item,
                    )
            await _save_receipt(
                command.idempotency_key,
                "rpg.remove_item",
                command.user_id,
                _inventory_result_to_json(result),
                connection,
            )
            return result


def _map_item(row: Sequence[object]) -> Item:
    """@brief 映射道具定义 / Map an item definition.

    @param row SQL 行 / SQL row.
    @return 道具定义 / Item definition.
    """

    return Item(
        item_id=_integer(row[0]),
        name=str(row[1]),
        item_type=ItemType(str(row[2])),
        effect=str(row[3]) if row[3] is not None else None,
        description=str(row[4]) if row[4] is not None else None,
        price=_integer(row[5]),
        use_limit=_integer(row[6]),
    )


async def _load_item(item_id: int, connection: AsyncConnection | None) -> Item | None:
    """@brief 读取道具定义 / Read an item definition.

    @param item_id 道具 ID / Item ID.
    @param connection 可选事务 / Optional transaction.
    @return 道具或 None / Item or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, name, type, effect, description, price, use_limit "
        "FROM game.rpg_items WHERE id = %s",
        (item_id,),
        connection=connection,
    )
    return _map_item(row) if row is not None else None


async def _load_inventory(
    user_id: int, connection: AsyncConnection | None
) -> tuple[InventoryEntry, ...]:
    """@brief 读取正数量背包条目 / Read positive inventory entries.

    @param user_id 玩家 ID / Player ID.
    @param connection 可选事务 / Optional transaction.
    @return 背包条目 / Inventory entries.
    """

    rows = await db_connection.fetch_all(
        "SELECT items.id, items.name, items.type, items.effect, items.description, "
        "items.price, items.use_limit, inventory.quantity, inventory.version "
        "FROM game.rpg_player_inventory AS inventory "
        "JOIN game.rpg_items AS items ON items.id = inventory.item_id "
        "WHERE inventory.user_id = %s ORDER BY inventory.id",
        (user_id,),
        connection=connection,
    )
    return tuple(
        InventoryEntry(_map_item(row[:7]), _integer(row[7]), _integer(row[8]))
        for row in rows
    )


async def _lock_inventory_gate(user_id: int, connection: AsyncConnection) -> None:
    """@brief 串行化一个用户的背包槽位与数量变化 / Serialize one user's inventory slot and quantity mutations.

    @param user_id 玩家 ID / Player ID.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    @note advisory gate 覆盖空背包没有可锁行的容量竞态 / The advisory gate covers capacity races when an empty inventory has no row to lock.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"rpg-inventory:{user_id}",),
        connection=connection,
    )


def _item_to_json(item: Item | None) -> object:
    """@brief 序列化可选道具 / Serialize an optional item.

    @param item 道具或 None / Item or None.
    @return JSON 值 / JSON value.
    """

    if item is None:
        return None
    return {
        "item_id": item.item_id,
        "name": item.name,
        "item_type": item.item_type.value,
        "effect": item.effect,
        "description": item.description,
        "price": item.price,
        "use_limit": item.use_limit,
    }


def _item_from_json(value: object) -> Item | None:
    """@brief 解析可选道具 / Parse an optional item.

    @param value JSON 值 / JSON value.
    @return 道具或 None / Item or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return Item(
        int(data["item_id"]),
        str(data["name"]),
        ItemType(str(data["item_type"])),
        str(data["effect"]) if data.get("effect") is not None else None,
        str(data["description"]) if data.get("description") is not None else None,
        int(data["price"]),
        int(data["use_limit"]),
    )


def _inventory_entry_to_json(entry: InventoryEntry) -> dict[str, object]:
    """@brief 序列化背包条目 / Serialize an inventory entry.

    @param entry 背包条目 / Inventory entry.
    @return JSON 对象 / JSON object.
    """

    return {
        "item": _item_to_json(entry.item),
        "quantity": entry.quantity,
        "version": entry.version,
    }


def _inventory_entry_from_json(value: object) -> InventoryEntry:
    """@brief 解析背包条目 / Parse an inventory entry.

    @param value JSON 值 / JSON value.
    @return 背包条目 / Inventory entry.
    """

    data = _json_object(value)
    item = _item_from_json(data["item"])
    if item is None:
        raise ValueError("Receipt inventory entry has no item")
    return InventoryEntry(item, int(data["quantity"]), int(data["version"]))


def _inventory_result_to_json(result: InventoryResult) -> dict[str, object]:
    """@brief 序列化背包回执 / Serialize an inventory receipt.

    @param result 背包结果 / Inventory result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "entries": [_inventory_entry_to_json(entry) for entry in result.entries],
        "item": _item_to_json(result.item),
    }


def _inventory_result_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> InventoryResult:
    """@brief 解析背包回执 / Parse an inventory receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return 背包结果 / Inventory result.
    """

    raw_entries = value.get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError("Receipt inventory entries must be an array")
    return InventoryResult(
        RpgCode(str(value["code"])),
        tuple(_inventory_entry_from_json(raw) for raw in raw_entries),
        _item_from_json(value.get("item")),
        replayed,
    )


__all__ = ["PostgresRpgInventoryOperations"]
