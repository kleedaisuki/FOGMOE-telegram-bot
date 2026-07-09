from fogmoe_bot.infrastructure.database import connection as db_connection

RPG_CHARACTER_UPDATE_FIELDS = {
    "level",
    "hp",
    "max_hp",
    "atk",
    "matk",
    "def",
    "experience",
    "allow_battle",
}
RPG_EQUIPMENT_SLOT_COLUMNS = {
    "weapon_id",
    "offhand_id",
    "armor_id",
    "treasure1_id",
    "treasure2_id",
}


async def fetch_user_omikuji(user_id: int, fortune_date, *, connection=None):
    """@brief 读取用户当日御神签 / Fetch user's daily omikuji.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param fortune_date 抽签日期 / Fortune date.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(fortune,)` 行；不存在时返回 None / Fortune row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT fortune FROM user_omikuji WHERE user_id = %s AND fortune_date = %s",
        (user_id, fortune_date),
        connection=connection,
    )


async def upsert_user_omikuji(user_id: int, fortune_date, fortune: str, *, connection=None) -> None:
    """@brief 写入用户御神签 / Upsert user's omikuji.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param fortune_date 抽签日期 / Fortune date.
    @param fortune 运势文本 / Fortune text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO user_omikuji (user_id, fortune_date, fortune) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, fortune_date) DO UPDATE SET fortune = EXCLUDED.fortune",
        (user_id, fortune_date, fortune),
        connection=connection,
    )


async def fetch_rpg_character(user_id: int, *, connection=None):
    """@brief 读取 RPG 角色 / Fetch RPG character.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT * FROM rpg_characters WHERE user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )


async def insert_rpg_character(user_id: int, *, connection=None) -> None:
    """@brief 创建 RPG 角色 / Insert RPG character.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO rpg_characters "
        "(user_id, level, hp, max_hp, atk, matk, def, experience, allow_battle) "
        "VALUES (%s, 1, 10, 10, 2, 0, 1, 0, TRUE)",
        (user_id,),
        connection=connection,
    )


async def update_rpg_character_stats(user_id: int, updates: dict, *, connection=None) -> int:
    """@brief 更新 RPG 角色属性 / Update RPG character stats.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param updates 属性更新字典 / Stat update dictionary.
    @param connection 可选数据库连接 / Optional database connection.
    @return 影响行数 / Affected row count.
    @note 只允许白名单字段 / Only allowlisted fields are accepted.
    """

    if not updates:
        return 0
    invalid_fields = set(updates) - RPG_CHARACTER_UPDATE_FIELDS
    if invalid_fields:
        raise ValueError(f"invalid RPG character fields: {sorted(invalid_fields)}")

    set_clause = ", ".join(f"{key} = %s" for key in updates)
    values = [updates[key] for key in updates]
    values.append(user_id)
    return await db_connection.execute(
        f"UPDATE rpg_characters SET {set_clause} WHERE user_id = %s",
        tuple(values),
        connection=connection,
    )


async def fetch_player_inventory(user_id: int, *, connection=None):
    """@brief 读取玩家背包 / Fetch player inventory.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行列表 / Mapping rows.
    """

    return await db_connection.fetch_all(
        "SELECT pi.id, pi.user_id, pi.item_id, pi.quantity, "
        "i.name, i.type, i.effect, i.description, i.price "
        "FROM rpg_player_inventory pi "
        "JOIN rpg_items i ON pi.item_id = i.id "
        "WHERE pi.user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )


async def fetch_rpg_item(item_id: int, *, connection=None):
    """@brief 读取 RPG 道具 / Fetch RPG item.

    @param item_id 道具 ID / Item ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT * FROM rpg_items WHERE id = %s",
        (item_id,),
        mapping=True,
        connection=connection,
    )


async def increment_inventory_item(user_id: int, item_id: int, quantity: int, *, connection=None) -> None:
    """@brief 增加背包道具数量 / Increment inventory item quantity.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param item_id 道具 ID / Item ID.
    @param quantity 增加数量 / Quantity to add.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE rpg_player_inventory SET quantity = quantity + %s "
        "WHERE user_id = %s AND item_id = %s",
        (quantity, user_id, item_id),
        connection=connection,
    )


async def insert_inventory_item(user_id: int, item_id: int, quantity: int, *, connection=None) -> None:
    """@brief 插入背包道具 / Insert inventory item.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param item_id 道具 ID / Item ID.
    @param quantity 数量 / Quantity.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO rpg_player_inventory (user_id, item_id, quantity) VALUES (%s, %s, %s)",
        (user_id, item_id, quantity),
        connection=connection,
    )


async def delete_inventory_item(user_id: int, item_id: int, *, connection=None) -> None:
    """@brief 删除背包道具 / Delete inventory item.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param item_id 道具 ID / Item ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "DELETE FROM rpg_player_inventory WHERE user_id = %s AND item_id = %s",
        (user_id, item_id),
        connection=connection,
    )


async def decrement_inventory_item(user_id: int, item_id: int, quantity: int, *, connection=None) -> None:
    """@brief 减少背包道具数量 / Decrement inventory item quantity.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param item_id 道具 ID / Item ID.
    @param quantity 减少数量 / Quantity to subtract.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE rpg_player_inventory SET quantity = quantity - %s "
        "WHERE user_id = %s AND item_id = %s",
        (quantity, user_id, item_id),
        connection=connection,
    )


async def fetch_player_equipment(user_id: int, *, connection=None):
    """@brief 读取玩家装备 / Fetch player equipment.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT pe.user_id, pe.weapon_id, pe.offhand_id, pe.armor_id, "
        "pe.treasure1_id, pe.treasure2_id, "
        "w.name as weapon_name, o.name as offhand_name, a.name as armor_name, "
        "t1.name as treasure1_name, t2.name as treasure2_name "
        "FROM rpg_player_equipment pe "
        "LEFT JOIN rpg_equipment w ON pe.weapon_id = w.id "
        "LEFT JOIN rpg_equipment o ON pe.offhand_id = o.id "
        "LEFT JOIN rpg_equipment a ON pe.armor_id = a.id "
        "LEFT JOIN rpg_equipment t1 ON pe.treasure1_id = t1.id "
        "LEFT JOIN rpg_equipment t2 ON pe.treasure2_id = t2.id "
        "WHERE pe.user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )


async def ensure_player_equipment(user_id: int, *, connection=None) -> None:
    """@brief 确保玩家装备行存在 / Ensure player equipment row exists.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO rpg_player_equipment (user_id) VALUES (%s)",
        (user_id,),
        connection=connection,
    )


async def fetch_rpg_equipment(equipment_id: int, *, connection=None):
    """@brief 读取 RPG 装备 / Fetch RPG equipment.

    @param equipment_id 装备 ID / Equipment ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT * FROM rpg_equipment WHERE id = %s",
        (equipment_id,),
        mapping=True,
        connection=connection,
    )


async def set_player_equipment_slot(
    user_id: int,
    slot_column: str,
    equipment_id: int | None,
    *,
    connection=None,
) -> int:
    """@brief 设置玩家装备槽 / Set player equipment slot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param slot_column 装备槽列名 / Equipment slot column.
    @param equipment_id 装备 ID；None 表示卸下 / Equipment ID, or None to unequip.
    @param connection 可选数据库连接 / Optional database connection.
    @return 影响行数 / Affected row count.
    @note slot_column 必须是白名单列 / slot_column must be allowlisted.
    """

    if slot_column not in RPG_EQUIPMENT_SLOT_COLUMNS:
        raise ValueError(f"invalid RPG equipment slot: {slot_column}")
    return await db_connection.execute(
        f"UPDATE rpg_player_equipment SET {slot_column} = %s WHERE user_id = %s",
        (equipment_id, user_id),
        connection=connection,
    )


async def insert_player_equipment_slot(
    user_id: int,
    slot_column: str,
    equipment_id: int,
    *,
    connection=None,
) -> None:
    """@brief 插入玩家装备槽 / Insert player equipment slot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param slot_column 装备槽列名 / Equipment slot column.
    @param equipment_id 装备 ID / Equipment ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    @note slot_column 必须是白名单列 / slot_column must be allowlisted.
    """

    if slot_column not in RPG_EQUIPMENT_SLOT_COLUMNS:
        raise ValueError(f"invalid RPG equipment slot: {slot_column}")
    await db_connection.execute(
        f"INSERT INTO rpg_player_equipment (user_id, {slot_column}) VALUES (%s, %s)",
        (user_id, equipment_id),
        connection=connection,
    )


async def upsert_player_equipment_stats(
    user_id: int,
    total_atk_bonus: int,
    total_def_bonus: int,
    total_hp_bonus: int,
    total_matk_bonus: int,
    *,
    connection=None,
) -> None:
    """@brief 写入玩家装备属性汇总 / Upsert player equipment stats.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param total_atk_bonus 总攻击加成 / Total attack bonus.
    @param total_def_bonus 总防御加成 / Total defense bonus.
    @param total_hp_bonus 总生命加成 / Total HP bonus.
    @param total_matk_bonus 总魔攻加成 / Total magic attack bonus.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO rpg_player_equipment_stats "
        "(user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET total_atk_bonus = EXCLUDED.total_atk_bonus, "
        "total_def_bonus = EXCLUDED.total_def_bonus, total_hp_bonus = EXCLUDED.total_hp_bonus, "
        "total_matk_bonus = EXCLUDED.total_matk_bonus, updated_at = CURRENT_TIMESTAMP",
        (user_id, total_atk_bonus, total_def_bonus, total_hp_bonus, total_matk_bonus),
        connection=connection,
    )


async def fetch_player_equipment_stats(user_id: int, *, connection=None):
    """@brief 读取玩家装备属性汇总 / Fetch player equipment stats.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT * FROM rpg_player_equipment_stats WHERE user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )
