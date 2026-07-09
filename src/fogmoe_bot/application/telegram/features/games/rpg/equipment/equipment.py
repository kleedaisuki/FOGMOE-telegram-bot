import logging
from typing import Dict, List, Optional, Union, Tuple

from fogmoe_bot.infrastructure.database import mysql_connection
from fogmoe_bot.infrastructure.database.repositories import game_repository


# --- 装备相关功能 ---
async def get_player_equipment(user_id: int) -> Dict:
    """获取玩家当前装备信息"""
    try:
        result = await game_repository.fetch_player_equipment(user_id)

        if not result:
            await game_repository.ensure_player_equipment(user_id)
            return {
                'user_id': user_id,
                'weapon_id': None,
                'offhand_id': None,
                'armor_id': None,
                'treasure1_id': None,
                'treasure2_id': None,
                'weapon_name': None,
                'offhand_name': None,
                'armor_name': None,
                'treasure1_name': None,
                'treasure2_name': None
            }

        return dict(result)
    except Exception as e:
        logging.error(f"获取玩家装备信息失败: {e}")
        return None


async def get_equipment_details(equipment_id: int) -> Dict:
    """获取装备详细信息"""
    if not equipment_id:
        return None

    try:
        result = await game_repository.fetch_rpg_equipment(equipment_id)
        return dict(result) if result else None
    except Exception as e:
        logging.error(f"获取装备详情失败: {e}")
        return None


async def equip_item(user_id: int, equipment_id: int) -> Tuple[bool, str]:
    """为玩家装备物品"""
    try:
        # 检查装备是否存在
        equipment = await get_equipment_details(equipment_id)
        if not equipment:
            return False, "装备不存在"
            
        equipment_type = equipment['type']
        
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False, "获取玩家装备信息失败"
            
        # 检查玩家是否拥有此装备（未来实现）
        # TODO: 检查玩家背包中是否有此装备
            
        # 确定要更新的装备槽位
        slot_column = f"{equipment_type}_id"
        if slot_column not in ['weapon_id', 'offhand_id', 'armor_id', 'treasure1_id', 'treasure2_id']:
            return False, f"不支持的装备类型: {equipment_type}"
            
        # 更新玩家装备
        async with mysql_connection.transaction() as connection:
            rows_affected = await game_repository.set_player_equipment_slot(
                user_id,
                slot_column,
                equipment_id,
                connection=connection,
            )
            if rows_affected == 0:
                await game_repository.insert_player_equipment_slot(
                    user_id,
                    slot_column,
                    equipment_id,
                    connection=connection,
                )

        result = (True, f"成功装备 {equipment['name']}")
        
        # 更新装备统计数据
        if result[0]:
            await update_equipment_stats(user_id)
            
        return result
                
    except Exception as e:
        logging.error(f"装备物品过程中出错: {e}")
        return False, f"装备出错: {str(e)}"


async def unequip_item(user_id: int, equipment_type: str) -> Tuple[bool, str]:
    """卸下玩家装备"""
    try:
        # 验证装备类型
        if equipment_type not in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            return False, f"不支持的装备类型: {equipment_type}"
            
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False, "获取玩家装备信息失败"
            
        # 检查该位置是否有装备
        slot_column = f"{equipment_type}_id"
        slot_name = f"{equipment_type}_name"
        
        if not current_equipment[slot_column]:
            return False, f"你当前没有装备{equipment_type_to_chinese(equipment_type)}"
            
        equipment_name = current_equipment[slot_name]
            
        # 更新玩家装备
        async with mysql_connection.transaction() as connection:
            await game_repository.set_player_equipment_slot(
                user_id,
                slot_column,
                None,
                connection=connection,
            )

        result = (True, f"成功卸下 {equipment_name}")
        
        # 更新装备统计数据
        if result[0]:
            await update_equipment_stats(user_id)
            
        return result
    except Exception as e:
        logging.error(f"卸下装备过程中出错: {e}")
        return False, f"卸下装备出错: {str(e)}"


async def update_equipment_stats(user_id: int) -> bool:
    """更新玩家装备带来的属性加成"""
    try:
        # 获取玩家当前装备
        current_equipment = await get_player_equipment(user_id)
        if not current_equipment:
            return False
            
        # 计算各项属性加成总和
        total_atk_bonus = 0
        total_def_bonus = 0
        total_hp_bonus = 0
        total_matk_bonus = 0
        
        # 检查每个装备槽位
        for slot in ['weapon', 'offhand', 'armor', 'treasure1', 'treasure2']:
            equipment_id = current_equipment[f"{slot}_id"]
            if equipment_id:
                equipment = await get_equipment_details(equipment_id)
                if equipment:
                    total_atk_bonus += equipment['atk_bonus']
                    total_def_bonus += equipment['def_bonus']
                    total_hp_bonus += equipment['hp_bonus']
                    total_matk_bonus += equipment['matk_bonus']
        
        # 更新装备统计缓存表
        async with mysql_connection.transaction() as connection:
            await game_repository.upsert_player_equipment_stats(
                user_id,
                total_atk_bonus,
                total_def_bonus,
                total_hp_bonus,
                total_matk_bonus,
                connection=connection,
            )
        return True
            
    except Exception as e:
        logging.error(f"更新装备统计数据时出错: {e}")
        return False


async def get_equipment_stats(user_id: int) -> Dict:
    """获取玩家装备的属性加成总和"""
    try:
        result = await game_repository.fetch_player_equipment_stats(user_id)

        if not result:
            return {
                'user_id': user_id,
                'total_atk_bonus': 0,
                'total_def_bonus': 0,
                'total_hp_bonus': 0,
                'total_matk_bonus': 0
            }

        return dict(result)
    except Exception as e:
        logging.error(f"获取装备属性加成数据失败: {e}")
        return None


def equipment_type_to_chinese(equipment_type: str) -> str:
    """将装备类型转换为中文描述"""
    type_map = {
        'weapon': '武器',
        'offhand': '副武器',
        'armor': '护甲',
        'treasure1': '宝物1',
        'treasure2': '宝物2'
    }
    return type_map.get(equipment_type, equipment_type) 
