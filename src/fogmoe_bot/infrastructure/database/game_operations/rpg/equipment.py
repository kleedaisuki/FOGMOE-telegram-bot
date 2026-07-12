"""@brief RPG 装备 PostgreSQL adapter / PostgreSQL adapter for RPG equipment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.rpg.equipment_models import (
    EquipItem,
    EquipmentResult,
    UnequipItem,
)
from fogmoe_bot.application.games.ports.rpg.equipment import RpgEquipmentOperations
from fogmoe_bot.domain.games import (
    Equipment,
    EquipmentLoadout,
    EquipmentSlot,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from ..common import (
    _integer,
    _json_object,
    _load_receipt,
    _lock_receipt_key,
    _save_receipt,
)
from .common import (
    _load_character,
)


class PostgresRpgEquipmentOperations(RpgEquipmentOperations):
    """@brief RPG 装备槽位、派生属性与回执 adapter / RPG equipment adapter for slots, derived statistics, and receipts."""

    async def equipment(self, user_id: int) -> EquipmentLoadout | None:
        """@brief 读取角色的装备快照 / Read a character's loadout snapshot.

        @param user_id 玩家 ID / Player ID.
        @return 装备快照；无角色为 None / Loadout, or None without a character.
        """

        character = await _load_character(user_id, None)
        if character is None:
            return None
        async with db_connection.transaction() as connection:
            await _ensure_equipment_row(user_id, connection)
            return await _load_equipment(user_id, connection)

    async def equipment_details(self, equipment_id: int) -> Equipment | None:
        """@brief 读取装备定义 / Read an equipment definition.

        @param equipment_id 装备 ID / Equipment ID.
        @return 装备或 None / Equipment or None.
        """

        return await _load_equipment_item(equipment_id, None)

    async def equip(self, command: EquipItem) -> EquipmentResult:
        """@brief 幂等设置装备槽并刷新派生统计 / Idempotently set a slot and refresh derived statistics.

        @param command 装备命令 / Equip command.
        @return 装备结果 / Equipment result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "rpg.equip", command.user_id, connection
            )
            if replay is not None:
                return _equipment_result_from_json(replay, replayed=True)
            character = await _load_character(command.user_id, connection)
            if character is None:
                result = EquipmentResult(RpgCode.NO_CHARACTER)
            else:
                equipment = await _load_equipment_item(command.equipment_id, connection)
                if equipment is None:
                    result = EquipmentResult(RpgCode.NOT_FOUND)
                else:
                    await _ensure_equipment_row(command.user_id, connection)
                    loadout = await _lock_equipment(command.user_id, connection)
                    if loadout is None:
                        raise RuntimeError("Ensured equipment row was not loadable")
                    affected = await db_connection.execute(
                        f"UPDATE game.rpg_player_equipment SET {equipment.slot.value}_id = %s, "
                        "version = version + 1 WHERE user_id = %s AND version = %s",
                        (
                            equipment.equipment_id,
                            command.user_id,
                            loadout.version,
                        ),
                        connection=connection,
                    )
                    if affected != 1:
                        raise RuntimeError("Equipment OCC update lost its locked row")
                    updated = await _load_equipment(command.user_id, connection)
                    if updated is None:
                        raise RuntimeError("Updated equipment row disappeared")
                    await _save_equipment_stats(updated, connection)
                    result = EquipmentResult(RpgCode.SUCCESS, updated, equipment)
            await _save_receipt(
                command.idempotency_key,
                "rpg.equip",
                command.user_id,
                _equipment_result_to_json(result),
                connection,
            )
            return result

    async def unequip(self, command: UnequipItem) -> EquipmentResult:
        """@brief 幂等清空装备槽并刷新派生统计 / Idempotently clear a slot and refresh derived statistics.

        @param command 卸下命令 / Unequip command.
        @return 装备结果 / Equipment result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key,
                "rpg.unequip",
                command.user_id,
                connection,
            )
            if replay is not None:
                return _equipment_result_from_json(replay, replayed=True)
            character = await _load_character(command.user_id, connection)
            if character is None:
                result = EquipmentResult(RpgCode.NO_CHARACTER)
            else:
                await _ensure_equipment_row(command.user_id, connection)
                loadout = await _lock_equipment(command.user_id, connection)
                if loadout is None:
                    raise RuntimeError("Ensured equipment row was not loadable")
                existing = loadout.item_at(command.slot)
                if existing is None:
                    result = EquipmentResult(RpgCode.EMPTY_SLOT, loadout)
                else:
                    affected = await db_connection.execute(
                        f"UPDATE game.rpg_player_equipment SET {command.slot.value}_id = NULL, "
                        "version = version + 1 WHERE user_id = %s AND version = %s",
                        (command.user_id, loadout.version),
                        connection=connection,
                    )
                    if affected != 1:
                        raise RuntimeError("Equipment OCC update lost its locked row")
                    updated = await _load_equipment(command.user_id, connection)
                    if updated is None:
                        raise RuntimeError("Updated equipment row disappeared")
                    await _save_equipment_stats(updated, connection)
                    result = EquipmentResult(RpgCode.SUCCESS, updated, existing)
            await _save_receipt(
                command.idempotency_key,
                "rpg.unequip",
                command.user_id,
                _equipment_result_to_json(result),
                connection,
            )
            return result


def _map_equipment_item(row: Sequence[object]) -> Equipment:
    """@brief 映射装备定义行 / Map an equipment-definition row.

    @param row SQL 行 / SQL row.
    @return 装备定义 / Equipment definition.
    """

    return Equipment(
        equipment_id=_integer(row[0]),
        name=str(row[1]),
        slot=EquipmentSlot(str(row[2])),
        attack_bonus=_integer(row[3]),
        defense_bonus=_integer(row[4]),
        hp_bonus=_integer(row[5]),
        magic_attack_bonus=_integer(row[6]),
        description=str(row[7]) if row[7] is not None else None,
        price=_integer(row[8]),
        rarity=_integer(row[9]),
    )


async def _load_equipment_item(
    equipment_id: int, connection: AsyncConnection | None
) -> Equipment | None:
    """@brief 读取装备定义 / Read an equipment definition.

    @param equipment_id 装备 ID / Equipment ID.
    @param connection 可选事务 / Optional transaction.
    @return 装备或 None / Equipment or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, name, type, atk_bonus, def_bonus, hp_bonus, matk_bonus, "
        "description, price, rarity FROM game.rpg_equipment WHERE id = %s",
        (equipment_id,),
        connection=connection,
    )
    return _map_equipment_item(row) if row is not None else None


async def _ensure_equipment_row(user_id: int, connection: AsyncConnection) -> None:
    """@brief 确保装备聚合头存在 / Ensure the loadout aggregate head exists.

    @param user_id 玩家 ID / Player ID.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO game.rpg_player_equipment (user_id, version) VALUES (%s, 0) "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
        connection=connection,
    )


async def _load_equipment(
    user_id: int,
    connection: AsyncConnection | None,
    *,
    for_update: bool = False,
) -> EquipmentLoadout | None:
    """@brief 读取可选加锁装备聚合 / Read an optionally locked loadout aggregate.

    @param user_id 玩家 ID / Player ID.
    @param connection 可选事务 / Optional transaction.
    @param for_update 是否行锁 / Whether to row-lock.
    @return 装备快照或 None / Loadout or None.
    """

    suffix = " FOR UPDATE OF player" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT player.user_id, player.version, player.weapon_id, weapon.id, "
        "weapon.name, weapon.type, weapon.atk_bonus, weapon.def_bonus, weapon.hp_bonus, "
        "weapon.matk_bonus, weapon.description, weapon.price, weapon.rarity, "
        "player.offhand_id, offhand.id, offhand.name, offhand.type, offhand.atk_bonus, "
        "offhand.def_bonus, offhand.hp_bonus, offhand.matk_bonus, offhand.description, "
        "offhand.price, offhand.rarity, player.armor_id, armor.id, armor.name, armor.type, "
        "armor.atk_bonus, armor.def_bonus, armor.hp_bonus, armor.matk_bonus, armor.description, "
        "armor.price, armor.rarity, player.treasure1_id, treasure1.id, treasure1.name, "
        "treasure1.type, treasure1.atk_bonus, treasure1.def_bonus, treasure1.hp_bonus, "
        "treasure1.matk_bonus, treasure1.description, treasure1.price, treasure1.rarity, "
        "player.treasure2_id, treasure2.id, treasure2.name, treasure2.type, "
        "treasure2.atk_bonus, treasure2.def_bonus, treasure2.hp_bonus, treasure2.matk_bonus, "
        "treasure2.description, treasure2.price, treasure2.rarity "
        "FROM game.rpg_player_equipment AS player "
        "LEFT JOIN game.rpg_equipment AS weapon ON weapon.id = player.weapon_id "
        "LEFT JOIN game.rpg_equipment AS offhand ON offhand.id = player.offhand_id "
        "LEFT JOIN game.rpg_equipment AS armor ON armor.id = player.armor_id "
        "LEFT JOIN game.rpg_equipment AS treasure1 ON treasure1.id = player.treasure1_id "
        "LEFT JOIN game.rpg_equipment AS treasure2 ON treasure2.id = player.treasure2_id "
        f"WHERE player.user_id = %s{suffix}",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    slots: list[tuple[EquipmentSlot, Equipment | None]] = []
    offsets = (2, 13, 24, 35, 46)
    for slot, offset in zip(EquipmentSlot, offsets, strict=True):
        item = (
            _map_equipment_item(row[offset + 1 : offset + 11])
            if row[offset] is not None
            else None
        )
        slots.append((slot, item))
    return EquipmentLoadout(_integer(row[0]), tuple(slots), _integer(row[1]))


async def _lock_equipment(
    user_id: int, connection: AsyncConnection
) -> EquipmentLoadout | None:
    """@brief 锁定装备聚合 / Lock a loadout aggregate.

    @param user_id 玩家 ID / Player ID.
    @param connection 活动事务 / Active transaction.
    @return 装备快照或 None / Loadout or None.
    """

    return await _load_equipment(user_id, connection, for_update=True)


async def _save_equipment_stats(
    loadout: EquipmentLoadout, connection: AsyncConnection
) -> None:
    """@brief 在同事务刷新装备派生缓存 / Refresh the derived loadout cache in the same transaction.

    @param loadout 装备快照 / Loadout snapshot.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    attack, defense, hp, magic_attack = loadout.bonuses
    await db_connection.execute(
        "INSERT INTO game.rpg_player_equipment_stats "
        "(user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET "
        "total_atk_bonus = EXCLUDED.total_atk_bonus, "
        "total_def_bonus = EXCLUDED.total_def_bonus, "
        "total_hp_bonus = EXCLUDED.total_hp_bonus, "
        "total_matk_bonus = EXCLUDED.total_matk_bonus, updated_at = CURRENT_TIMESTAMP",
        (loadout.user_id, attack, defense, hp, magic_attack),
        connection=connection,
    )


def _equipment_item_to_json(equipment: Equipment | None) -> object:
    """@brief 序列化可选装备 / Serialize optional equipment.

    @param equipment 装备或 None / Equipment or None.
    @return JSON 值 / JSON value.
    """

    if equipment is None:
        return None
    return {
        "equipment_id": equipment.equipment_id,
        "name": equipment.name,
        "slot": equipment.slot.value,
        "attack_bonus": equipment.attack_bonus,
        "defense_bonus": equipment.defense_bonus,
        "hp_bonus": equipment.hp_bonus,
        "magic_attack_bonus": equipment.magic_attack_bonus,
        "description": equipment.description,
        "price": equipment.price,
        "rarity": equipment.rarity,
    }


def _equipment_item_from_json(value: object) -> Equipment | None:
    """@brief 解析可选装备 / Parse optional equipment.

    @param value JSON 值 / JSON value.
    @return 装备或 None / Equipment or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return Equipment(
        int(data["equipment_id"]),
        str(data["name"]),
        EquipmentSlot(str(data["slot"])),
        int(data["attack_bonus"]),
        int(data["defense_bonus"]),
        int(data["hp_bonus"]),
        int(data["magic_attack_bonus"]),
        str(data["description"]) if data.get("description") is not None else None,
        int(data["price"]),
        int(data["rarity"]),
    )


def _loadout_to_json(loadout: EquipmentLoadout | None) -> object:
    """@brief 序列化可选装备聚合 / Serialize an optional loadout aggregate.

    @param loadout 装备快照 / Loadout snapshot.
    @return JSON 值 / JSON value.
    """

    if loadout is None:
        return None
    return {
        "user_id": loadout.user_id,
        "version": loadout.version,
        "slots": {
            slot.value: _equipment_item_to_json(item) for slot, item in loadout.slots
        },
    }


def _loadout_from_json(value: object) -> EquipmentLoadout | None:
    """@brief 解析可选装备聚合 / Parse an optional loadout aggregate.

    @param value JSON 值 / JSON value.
    @return 装备快照或 None / Loadout or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    slots_data = _json_object(data["slots"])
    return EquipmentLoadout(
        int(data["user_id"]),
        tuple(
            (slot, _equipment_item_from_json(slots_data.get(slot.value)))
            for slot in EquipmentSlot
        ),
        int(data["version"]),
    )


def _equipment_result_to_json(result: EquipmentResult) -> dict[str, object]:
    """@brief 序列化装备用例回执 / Serialize an equipment receipt.

    @param result 装备结果 / Equipment result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "loadout": _loadout_to_json(result.loadout),
        "equipment": _equipment_item_to_json(result.equipment),
    }


def _equipment_result_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> EquipmentResult:
    """@brief 解析装备用例回执 / Parse an equipment receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return 装备结果 / Equipment result.
    """

    return EquipmentResult(
        RpgCode(str(value["code"])),
        _loadout_from_json(value.get("loadout")),
        _equipment_item_from_json(value.get("equipment")),
        replayed,
    )


__all__ = ["PostgresRpgEquipmentOperations"]
