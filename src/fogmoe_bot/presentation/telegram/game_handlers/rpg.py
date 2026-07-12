"""@brief RPG Telegram 适配器 / Telegram adapter for RPG commands."""

from __future__ import annotations

from datetime import timedelta
from telegram import Update

from fogmoe_bot.application.games.rpg.character_models import (
    FightMonster,
    FightPlayer,
    MonsterBattleResult,
    PlayerBattleResult,
    SetBattleAllowance,
)
from fogmoe_bot.application.games.rpg.common import RpgCode
from fogmoe_bot.application.games.rpg.character_service import (
    RPG_CHARACTER_SERVICE_DATA_KEY,
    RpgCharacterService,
)
from fogmoe_bot.application.games.ports.rpg.equipment import (
    RPG_EQUIPMENT_OPERATIONS_DATA_KEY,
    RpgEquipmentOperations,
)
from fogmoe_bot.application.games.rpg.equipment_models import (
    EquipItem,
    UnequipItem,
)
from fogmoe_bot.application.games.rpg.inventory_models import (
    UseItem,
)
from fogmoe_bot.application.games.rpg.inventory_service import (
    RPG_INVENTORY_SERVICE_DATA_KEY,
    RpgInventoryService,
)
from fogmoe_bot.domain.games import (
    BattleResult,
    Character,
    EQUIPMENT_SLOT_NAMES,
    EquipmentLoadout,
    EquipmentSlot,
    ITEM_TYPE_NAMES,
    LevelUp,
    Monster,
    MonsterBattle,
)

from .common import TelegramContext, current_time, idempotency_key

RPG_HELP_TEXT = """
🎮 RPG游戏系统命令

基础命令:
/rpg - 查看角色状态
/rpg help - 显示此帮助信息

战斗系统:
/rpg battle <用户名> - 与其他玩家战斗(每小时限制1次)
/rpg battle monster <怪物ID> - 与怪物战斗
/rpg battle on|off - 开启/关闭被挑战功能
/rpg monsters - 查看可挑战的怪物列表
/rpg heal - 恢复生命值

装备系统:
/rpg equip - 查看当前装备
/rpg equip <装备ID> - 装备指定物品
/rpg equip unequip <类型> - 卸下指定类型装备

道具系统:
/rpg item - 查看道具栏
/rpg item <道具ID> - 查看道具详情
/rpg item use <道具ID> - 使用道具

每场战斗后，您需要恢复生命值才能再次挑战。
与怪物战斗有5分钟冷却时间。
与玩家战斗胜利可获得对方部分金币和经验值。
击败怪物可获得固定的金币和经验奖励。
""".strip()
"""@brief RPG 用户帮助文本 / RPG user help text."""


def _character_service(context: TelegramContext) -> RpgCharacterService:
    """@brief 读取 RPG 角色 capability / Read the RPG character capability."""

    value = context.application.bot_data.get(RPG_CHARACTER_SERVICE_DATA_KEY)
    if not isinstance(value, RpgCharacterService):
        raise RuntimeError("RPG character service was not assembled")
    return value


def _equipment_operations(context: TelegramContext) -> RpgEquipmentOperations:
    """@brief 读取 RPG 装备原子端口 / Read the atomic RPG equipment port."""

    value = context.application.bot_data.get(RPG_EQUIPMENT_OPERATIONS_DATA_KEY)
    if not isinstance(value, RpgEquipmentOperations):
        raise RuntimeError("RPG equipment operations were not assembled")
    return value


def _inventory_service(context: TelegramContext) -> RpgInventoryService:
    """@brief 读取 RPG 库存 capability / Read the RPG inventory capability."""

    value = context.application.bot_data.get(RPG_INVENTORY_SERVICE_DATA_KEY)
    if not isinstance(value, RpgInventoryService):
        raise RuntimeError("RPG inventory service was not assembled")
    return value


async def rpg_command_handler(update: Update, context: TelegramContext) -> None:
    """@brief 解析 /rpg 子命令并调用类型化服务 / Parse /rpg subcommands and call typed services.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    args = tuple(context.args or ())
    if not args:
        profile = await _character_service(context).ensure_profile(user.id)
        if profile.code is RpgCode.NOT_REGISTERED:
            await message.reply_text(
                "需要先在系统中记录你的信息，请尝试使用 /me 命令。"
            )
        elif profile.character is not None:
            prefix = "欢迎你，角色已创建。\n\n" if profile.created else ""
            await message.reply_text(
                prefix
                + _render_profile(
                    user.username or user.first_name,
                    profile.character,
                    profile.balance or 0,
                )
            )
        return
    command = args[0].lower()
    if command == "help":
        await message.reply_text(RPG_HELP_TEXT)
    elif command == "monsters":
        await message.reply_text(_render_monsters(_character_service(context).monsters))
    elif command == "heal":
        await _handle_heal(update, context)
    elif command == "battle":
        await _handle_battle(update, context, args[1:])
    elif command in {"equipment", "equip"}:
        await _handle_equipment(update, context, args[1:])
    elif command in {"inventory", "item"}:
        await _handle_inventory(update, context, args[1:])
    else:
        await message.reply_text(
            f"未知命令: {command}\n请使用 /rpg help 查看可用命令。"
        )


def _render_profile(name: str, character: Character, balance: int) -> str:
    """@brief 渲染角色状态 / Render character status.

    @param name 玩家展示名 / Player display name.
    @param character 角色对象 / Character object.
    @param balance 金币余额 / Coin balance.
    @return 状态文本 / Status text.
    """

    progress, required = character.experience_progress
    return (
        f"🎮 冒险者 {name}，你的状态如下：\n\n"
        f"📊 角色状态\n等级: {character.computed_level} (经验: {progress}/{required})\n"
        f"❤️ 生命值: {character.hp}/{character.max_hp}\n"
        f"⚔️ 攻击力: {character.attack} | 🔮 魔法攻击: {character.magic_attack}\n"
        f"🛡️ 防御力: {character.defense}\n🪙 金币: {balance}\n"
        f"🤺 允许被挑战: {'✅' if character.allow_battle else '❌'}\n\n"
        "📝 常用指令\n/rpg help - 查看所有指令"
    )


def _render_monsters(monsters: tuple[Monster, ...]) -> str:
    """@brief 渲染怪物目录 / Render the monster catalog.

    @return 怪物文本 / Monster text.
    """

    blocks = ["🎮 可挑战的怪物列表 🎮"]
    for monster in monsters:
        blocks.append(
            f"{monster.name} (ID: {monster.monster_id})\n"
            f"等级: {monster.level}\n生命值: {monster.hp}\n"
            f"攻击力: {monster.attack}\n防御力: {monster.defense}\n"
            f"经验奖励: {monster.experience_reward}\n金币奖励: {monster.coin_reward}\n"
            f"描述: {monster.description}"
        )
    blocks.append("使用 /rpg battle monster <怪物ID> 来挑战怪物。")
    return "\n\n".join(blocks)


async def _handle_heal(update: Update, context: TelegramContext) -> None:
    """@brief 处理 RPG 治疗 / Handle RPG healing.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    result = await _character_service(context).heal(
        user.id, idempotency_key=idempotency_key(update, "rpg:heal", user.id)
    )
    if result.code is RpgCode.NO_CHARACTER:
        await message.reply_text("你还没有创建角色，请先使用 /rpg 命令创建角色。")
    elif result.code is RpgCode.ALREADY_FULL_HP:
        await message.reply_text("你的生命值已经是满的了！")
    elif result.code is RpgCode.INSUFFICIENT_COINS:
        await message.reply_text(
            f"恢复生命值需要 10 金币，但你只有 {result.balance or 0} 金币。"
        )
    elif result.code is RpgCode.SUCCESS and result.character is not None:
        await message.reply_text(
            f"花费 10 金币恢复了生命值！\n当前HP: "
            f"{result.character.hp}/{result.character.max_hp}"
        )


async def _handle_battle(
    update: Update, context: TelegramContext, args: tuple[str, ...]
) -> None:
    """@brief 处理 RPG 战斗子命令 / Handle RPG battle subcommands.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @param args battle 后参数 / Arguments after battle.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if not args:
        await message.reply_text(
            "用法:\n/rpg battle <用户名> - 与玩家对战\n"
            "/rpg battle monster <怪物ID> - 与怪物对战\n"
            "/rpg battle on|off - 开启/关闭被挑战功能"
        )
        return
    action = args[0].lower()
    if action in {"on", "off"}:
        allowance_result = await _character_service(context).set_battle_allowance(
            SetBattleAllowance(
                user.id,
                action == "on",
                idempotency_key(update, "rpg:allow", user.id),
            )
        )
        if allowance_result.code is RpgCode.NO_CHARACTER:
            await message.reply_text("你还没有创建角色，请先使用 /rpg 创建。")
        elif allowance_result.code is RpgCode.SUCCESS:
            await message.reply_text(
                f"已将你的状态设置为 {'允许' if action == 'on' else '禁止'} 被挑战。"
            )
        return
    if action == "monster":
        if len(args) < 2:
            await message.reply_text("用法: /rpg battle monster <怪物ID>")
            return
        monster_result = await _character_service(context).fight_monster(
            FightMonster(
                user.id,
                user.username or user.first_name,
                args[1].lower(),
                current_time(),
                idempotency_key(update, "rpg:monster", user.id),
            )
        )
        await message.reply_text(_render_monster_battle(monster_result))
        return
    player_result = await _character_service(context).fight_player(
        FightPlayer(
            user.id,
            user.username or user.first_name,
            args[0],
            current_time(),
            idempotency_key(update, "rpg:player", user.id),
        )
    )
    await message.reply_text(_render_player_battle(player_result))


def _cooldown_text(duration: timedelta | None) -> str:
    """@brief 格式化冷却 / Format a cooldown.

    @param duration 剩余时长 / Remaining duration.
    @return 中文时长 / Chinese duration.
    """

    seconds = max(0, int((duration or timedelta()).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分钟" if hours else f"{minutes}分{seconds}秒"


def _render_monster_battle(result: MonsterBattleResult) -> str:
    """@brief 渲染 PVE 结果 / Render a PVE result.

    @param result PVE 用例结果 / PVE use-case result.
    @return 文本 / Text.
    """

    if result.code is RpgCode.NOT_FOUND:
        return "找不到该怪物。使用 /rpg monsters 查看所有可挑战的怪物。"
    if result.code is RpgCode.COOLDOWN:
        return f"你需要休息一下！还需要等待 {_cooldown_text(result.cooldown_remaining)} 才能再次挑战怪物。"
    if result.code is RpgCode.NO_CHARACTER:
        return "你还没有创建角色，请先使用 /rpg 命令创建。"
    if result.code is RpgCode.DEAD:
        return "你的生命值过低，无法发起战斗！先使用 /rpg heal 恢复生命值。"
    if result.battle is None or result.monster is None:
        return "战斗处理失败，请稍后再试。"
    text = _render_battle_turns(result.battle, result.monster.name)
    if result.battle.result is BattleResult.WIN:
        text += (
            f"\n\n🎁 战斗奖励:\n获得 {result.experience_reward} 点经验值\n"
            f"获得 {result.coin_reward} 枚金币"
        )
    elif result.battle.result is BattleResult.LOSE:
        text += "\n\n😢 战斗失败。使用 /rpg heal 恢复生命值，然后再尝试挑战吧！"
    return text + _render_level_up(result.level_up)


def _render_battle_turns(battle: MonsterBattle, monster_name: str) -> str:
    """@brief 渲染 PVE 动作 / Render PVE actions.

    @param battle PVE 过程 / PVE process.
    @param monster_name 怪物名 / Monster name.
    @return 战斗日志 / Battle log.
    """

    lines = [
        f"回合 {turn.number}: {turn.attacker} 对 {turn.defender} 造成了 "
        f"{turn.damage} 点伤害，剩余 HP: {turn.remaining_hp}"
        for turn in battle.turns
    ]
    outcome = {
        BattleResult.WIN: f"胜利！你击败了 {monster_name}。",
        BattleResult.LOSE: f"失败！你被 {monster_name} 击败了。",
        BattleResult.DRAW: "战斗时间过长，以平局结束！",
    }[battle.result]
    return "\n".join((*lines, f"\n战斗结果: {outcome}"))


def _render_player_battle(result: PlayerBattleResult) -> str:
    """@brief 渲染 PVP 结果 / Render a PVP result.

    @param result PVP 用例结果 / PVP use-case result.
    @return 文本 / Text.
    """

    errors = {
        RpgCode.TARGET_NOT_FOUND: "找不到该用户名的玩家。请确保输入正确（区分大小写，不含@）。",
        RpgCode.SELF_TARGET: "你不能挑战自己！",
        RpgCode.NO_CHARACTER: "你还没有创建角色，请先使用 /rpg 创建。",
        RpgCode.DEAD: "你当前生命值过低，无法发起战斗！使用 /rpg heal 恢复生命值。",
        RpgCode.TARGET_NO_CHARACTER: "目标玩家还没有创建 RPG 角色。",
        RpgCode.TARGET_DISALLOWS: "目标玩家当前设置了不允许被挑战。",
        RpgCode.TARGET_DEAD: "目标玩家当前生命值过低，无法接受挑战！",
    }
    if result.code is RpgCode.COOLDOWN:
        return f"你需要休息一下！还需要等待 {_cooldown_text(result.cooldown_remaining)} 才能再次挑战其他玩家。"
    if result.code in errors:
        return errors[result.code]
    if result.battle is None:
        return "战斗处理失败，请稍后再试。"
    lines = [
        f"回合 {turn.number}: {turn.attacker} 对 {turn.defender} 造成了 "
        f"{turn.damage} 点伤害，剩余 HP: {turn.remaining_hp}"
        for turn in result.battle.turns
    ]
    if result.battle.is_draw:
        lines.append("\n战斗结果：平局！")
    else:
        lines.append(f"\n战斗结果：{result.winner_name} 获胜！")
        lines.append(
            f"\n--- 战后结算 ---\n{result.loser_name} 损失了 {result.coins_lost} 🪙 金币。\n"
            f"{result.winner_name} 获得了 {result.coins_awarded} 🪙 金币。\n"
            f"{result.winner_name} 获得了 {result.experience_awarded} 点经验值！"
        )
    return "\n".join(lines) + _render_level_up(result.level_up)


def _render_level_up(level_up: LevelUp | None) -> str:
    """@brief 渲染可选升级事件 / Render an optional level-up event.

    @param level_up 升级事件 / Level-up event.
    @return 可拼接文本 / Appendable text.
    """

    if level_up is None:
        return ""
    return (
        f"\n\n🎉 恭喜你升到了 {level_up.new_level} 级！\n"
        f"HP: +{level_up.hp_increase}, ATK: +{level_up.attack_increase}, "
        f"DEF: +{level_up.defense_increase}"
    )


async def _require_existing_character(update: Update, context: TelegramContext) -> bool:
    """@brief 检查子命令所需既有角色 / Check the existing-character prerequisite for subcommands.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return 有角色为 True / True when a character exists.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    profile = await _character_service(context).profile(user.id)
    if profile.code is not RpgCode.SUCCESS:
        await message.reply_text("你还没有创建角色，请先使用 /rpg 创建。")
        return False
    return True


async def _handle_equipment(
    update: Update, context: TelegramContext, args: tuple[str, ...]
) -> None:
    """@brief 处理装备子命令 / Handle equipment subcommands.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @param args equip 后参数 / Arguments after equip.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if (
        user is None
        or message is None
        or not await _require_existing_character(update, context)
    ):
        return
    operations = _equipment_operations(context)
    if not args:
        loadout = await operations.equipment(user.id)
        await message.reply_text(
            _render_loadout(user.username or user.first_name, loadout)
        )
        return
    if len(args) == 1 and args[0].isdigit():
        result = await operations.equip(
            EquipItem(
                user.id,
                int(args[0]),
                idempotency_key(update, "rpg:equip", user.id),
            )
        )
        await message.reply_text(
            f"成功装备 {result.equipment.name}"
            if result.code is RpgCode.SUCCESS and result.equipment is not None
            else "装备不存在"
        )
        return
    if len(args) == 2 and args[0].lower() == "unequip":
        try:
            slot = EquipmentSlot(args[1].lower())
        except ValueError:
            await message.reply_text(f"不支持的装备类型: {args[1]}")
            return
        result = await operations.unequip(
            UnequipItem(
                user.id,
                slot,
                idempotency_key(update, "rpg:unequip", user.id),
            )
        )
        if result.code is RpgCode.EMPTY_SLOT:
            await message.reply_text(f"你当前没有装备{EQUIPMENT_SLOT_NAMES[slot]}")
        elif result.code is RpgCode.SUCCESS and result.equipment is not None:
            await message.reply_text(f"成功卸下 {result.equipment.name}")
        return
    await message.reply_text(
        "装备命令用法：\n/rpg equip - 查看当前装备\n"
        "/rpg equip [装备ID] - 装备指定物品\n"
        "/rpg equip unequip [类型] - 卸下指定类型装备"
    )


def _render_loadout(name: str, loadout: EquipmentLoadout | None) -> str:
    """@brief 渲染装备快照 / Render a loadout snapshot.

    @param name 玩家名 / Player name.
    @param loadout 装备快照 / Loadout snapshot.
    @return 文本 / Text.
    """

    if loadout is None:
        return "获取装备信息失败，请稍后再试。"
    lines = [
        f"{EQUIPMENT_SLOT_NAMES[slot]}: {item.name if item is not None else '无'}"
        for slot, item in loadout.slots
    ]
    return (
        f"📦 {name} 的装备\n\n"
        + "\n".join(lines)
        + "\n\n使用 /rpg equip [装备ID] 装备物品\n"
        "使用 /rpg equip unequip [类型] 卸下装备"
    )


async def _handle_inventory(
    update: Update, context: TelegramContext, args: tuple[str, ...]
) -> None:
    """@brief 处理背包子命令 / Handle inventory subcommands.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @param args item 后参数 / Arguments after item.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if (
        user is None
        or message is None
        or not await _require_existing_character(update, context)
    ):
        return
    service = _inventory_service(context)
    if not args:
        entries = await service.inventory(user.id)
        if not entries:
            await message.reply_text(
                f"🎒 {user.username or user.first_name} 的道具栏 (0/10)\n\n道具栏空空如也..."
            )
        else:
            lines = [
                f"[{entry.item.item_id}] {entry.item.name} x{entry.quantity} "
                f"({ITEM_TYPE_NAMES[entry.item.item_type]})"
                for entry in entries
            ]
            await message.reply_text(
                f"🎒 {user.username or user.first_name} 的道具栏 ({len(entries)}/10)\n\n"
                + "\n".join(lines)
                + "\n\n使用 /rpg item use [道具ID] 使用道具"
            )
        return
    if len(args) == 2 and args[0].lower() == "use" and args[1].isdigit():
        result = await service.use(
            UseItem(
                user.id,
                int(args[1]),
                idempotency_key(update, "rpg:use-item", user.id),
            )
        )
        messages = {
            RpgCode.NOT_FOUND: "道具不存在",
            RpgCode.NOT_OWNED: "你没有这个道具",
            RpgCode.WRONG_ITEM_TYPE: (
                f"{result.item.name if result.item else '该道具'} 不是可使用的消耗品"
            ),
        }
        await message.reply_text(
            f"使用了 {result.item.name}"
            if result.code is RpgCode.SUCCESS and result.item is not None
            else messages.get(result.code, "使用道具失败")
        )
        return
    if len(args) == 1 and args[0].isdigit():
        item = await service.details(int(args[0]))
        if item is None:
            await message.reply_text(f"找不到ID为 {args[0]} 的道具。")
        else:
            await message.reply_text(
                f"🔍 道具详情: {item.name}\n\n类型: {ITEM_TYPE_NAMES[item.item_type]}\n"
                f"描述: {item.description or ''}\n效果: {item.effect or ''}\n"
                f"价值: {item.price} 金币\n\n使用 /rpg item use [道具ID] 使用此道具"
            )
        return
    await message.reply_text(
        "道具命令用法：\n/rpg item - 查看道具栏\n"
        "/rpg item [道具ID] - 查看道具详情\n"
        "/rpg item use [道具ID] - 使用道具"
    )
