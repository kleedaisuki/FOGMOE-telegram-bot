"""@brief RPG adapters 共享角色持久化与回执映射 primitives / Character persistence and receipt-mapping primitives shared by RPG adapters."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.games import Character, LevelUp
from fogmoe_bot.infrastructure.database import connection as db_connection

from ..common import _integer, _json_object


def _map_character(row: Sequence[object]) -> Character:
    """@brief 映射角色行 / Map a character row.

    @param row SQL 行 / SQL row.
    @return 角色聚合 / Character aggregate.
    """

    return Character(
        user_id=_integer(row[0]),
        level=_integer(row[1]),
        hp=_integer(row[2]),
        max_hp=_integer(row[3]),
        attack=_integer(row[4]),
        magic_attack=_integer(row[5]),
        defense=_integer(row[6]),
        experience=_integer(row[7]),
        allow_battle=bool(row[8]),
        version=_integer(row[9]),
    )


async def _load_character(
    user_id: int, connection: AsyncConnection | None
) -> Character | None:
    """@brief 读取角色 / Read a character.

    @param user_id 玩家 ID / Player ID.
    @param connection 可选事务 / Optional transaction.
    @return 角色或 None / Character or None.
    """

    row = await db_connection.fetch_one(
        "SELECT user_id, level, hp, max_hp, atk, matk, def, experience, "
        "allow_battle, version FROM game.rpg_characters WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return _map_character(row) if row is not None else None


async def _lock_character(
    user_id: int, connection: AsyncConnection
) -> Character | None:
    """@brief 锁定角色 / Lock a character.

    @param user_id 玩家 ID / Player ID.
    @param connection 活动事务 / Active transaction.
    @return 角色或 None / Character or None.
    """

    row = await db_connection.fetch_one(
        "SELECT user_id, level, hp, max_hp, atk, matk, def, experience, "
        "allow_battle, version FROM game.rpg_characters WHERE user_id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    return _map_character(row) if row is not None else None


async def _lock_characters(
    user_ids: Sequence[int], connection: AsyncConnection
) -> dict[int, Character]:
    """@brief 按用户 ID 升序锁角色 / Lock characters in ascending user-ID order.

    @param user_ids 玩家 ID / Player IDs.
    @param connection 活动事务 / Active transaction.
    @return ID 到角色映射 / ID-to-character mapping.
    """

    characters: dict[int, Character] = {}
    for user_id in sorted(set(user_ids)):
        character = await _lock_character(user_id, connection)
        if character is not None:
            characters[user_id] = character
    return characters


async def _save_character(
    character: Character,
    expected_version: int,
    connection: AsyncConnection,
) -> None:
    """@brief 用 OCC 保存角色聚合 / Persist a character aggregate using OCC.

    @param character 新角色 / New character.
    @param expected_version 旧版本 / Previous version.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    affected = await db_connection.execute(
        "UPDATE game.rpg_characters SET level = %s, hp = %s, max_hp = %s, "
        "atk = %s, matk = %s, def = %s, experience = %s, allow_battle = %s, "
        "version = %s WHERE user_id = %s AND version = %s",
        (
            character.level,
            character.hp,
            character.max_hp,
            character.attack,
            character.magic_attack,
            character.defense,
            character.experience,
            character.allow_battle,
            character.version,
            character.user_id,
            expected_version,
        ),
        connection=connection,
    )
    if affected != 1:
        raise RuntimeError("Character OCC update lost its locked row")


def _character_to_json(character: Character | None) -> object:
    """@brief 序列化可选角色 / Serialize an optional character.

    @param character 角色或 None / Character or None.
    @return JSON 值 / JSON value.
    """

    if character is None:
        return None
    return {
        "user_id": character.user_id,
        "level": character.level,
        "hp": character.hp,
        "max_hp": character.max_hp,
        "attack": character.attack,
        "magic_attack": character.magic_attack,
        "defense": character.defense,
        "experience": character.experience,
        "allow_battle": character.allow_battle,
        "version": character.version,
    }


def _character_from_json(value: object) -> Character | None:
    """@brief 解析可选角色 / Parse an optional character.

    @param value JSON 值 / JSON value.
    @return 角色或 None / Character or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return Character(
        int(data["user_id"]),
        int(data["level"]),
        int(data["hp"]),
        int(data["max_hp"]),
        int(data["attack"]),
        int(data["magic_attack"]),
        int(data["defense"]),
        int(data["experience"]),
        bool(data["allow_battle"]),
        int(data["version"]),
    )


def _level_up_to_json(level_up: LevelUp | None) -> object:
    """@brief 序列化可选升级事件 / Serialize an optional level-up event.

    @param level_up 升级事件 / Level-up event.
    @return JSON 值 / JSON value.
    """

    if level_up is None:
        return None
    return {
        "old_level": level_up.old_level,
        "new_level": level_up.new_level,
        "hp_increase": level_up.hp_increase,
        "attack_increase": level_up.attack_increase,
        "defense_increase": level_up.defense_increase,
    }


def _level_up_from_json(value: object) -> LevelUp | None:
    """@brief 解析可选升级事件 / Parse an optional level-up event.

    @param value JSON 值 / JSON value.
    @return 升级事件或 None / Level-up event or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return LevelUp(
        int(data["old_level"]),
        int(data["new_level"]),
        int(data["hp_increase"]),
        int(data["attack_increase"]),
        int(data["defense_increase"]),
    )
