"""@brief RPG 聚合、战斗与库存规则 / RPG aggregates, battle, and inventory rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Final

MAX_LEVEL: Final = 1000
"""@brief 旧系统的安全等级上限 / Legacy safety level cap."""

MAX_BATTLE_TURNS: Final = 20
"""@brief 单场战斗最大攻击动作数 / Maximum attack actions per battle."""

INVENTORY_CAPACITY: Final = 10
"""@brief 不同道具栏位上限 / Maximum distinct inventory slots."""


def experience_threshold(level: int) -> int:
    """@brief 返回进入下一等级的累计经验阈值 / Return cumulative experience needed to leave a level.

    @param level 当前等级 / Current level.
    @return 累计经验阈值 / Cumulative threshold.
    """

    if level <= 0:
        return 0
    return 50 * level**2 + 50 * level


def level_from_experience(experience: int) -> int:
    """@brief 将累计经验映射为等级 / Map cumulative experience to a level.

    @param experience 非负累计经验 / Non-negative cumulative experience.
    @return ``[1, 1000]`` 内等级 / Level in ``[1, 1000]``.
    """

    if experience < 0:
        return 1
    level = 1
    while level < MAX_LEVEL and experience >= experience_threshold(level):
        level += 1
    return level


def experience_gain(winner_level: int, loser_level: int) -> int:
    """@brief 按旧等级差公式计算 PVP 经验 / Calculate PVP experience using the legacy level-gap formula.

    @param winner_level 胜者等级 / Winner level.
    @param loser_level 负者等级 / Loser level.
    @return 至少一点经验 / At least one experience point.
    """

    level_difference = loser_level - winner_level
    if level_difference >= 10:
        multiplier = 2.0
    elif level_difference <= -10:
        multiplier = 0.1
    else:
        multiplier = max(0.1, min(2.0, 1.05 + level_difference / 10 * 0.95))
    return max(1, math.floor(50 * multiplier))


def physical_damage(attack: int, defense: int) -> int:
    """@brief 计算旧物理伤害 / Calculate legacy physical damage.

    @param attack 攻击力 / Attack value.
    @param defense 防御力 / Defense value.
    @return 不小于零的伤害 / Non-negative damage.
    """

    return max(0, attack - defense)


def magical_damage(magic_attack: int, defense: int) -> float:
    """@brief 计算旧魔法伤害 / Calculate legacy magical damage.

    @param magic_attack 魔法攻击 / Magic attack value.
    @param defense 防御力 / Defense value.
    @return 保留一位小数且不小于零的伤害 / Non-negative damage rounded to one decimal.
    """

    return round(max(0.0, magic_attack - defense / 2), 1)


@dataclass(frozen=True, slots=True)
class Character:
    """@brief RPG 角色聚合 / RPG character aggregate.

    @param user_id 玩家 ID / Player ID.
    @param level 持久化等级 / Persisted level.
    @param hp 当前生命值 / Current HP.
    @param max_hp 最大生命值 / Maximum HP.
    @param attack 物理攻击力 / Physical attack.
    @param magic_attack 魔法攻击力 / Magic attack.
    @param defense 防御力 / Defense.
    @param experience 累计经验 / Cumulative experience.
    @param allow_battle 是否允许被挑战 / Whether challenges are allowed.
    @param version OCC 版本 / OCC version.
    """

    user_id: int
    level: int = 1
    hp: int = 10
    max_hp: int = 10
    attack: int = 2
    magic_attack: int = 0
    defense: int = 1
    experience: int = 0
    allow_battle: bool = True
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 校验角色不变量 / Validate character invariants.

        @return None / None.
        """

        if self.user_id <= 0 or self.level <= 0 or self.version < 0:
            raise ValueError("Invalid character identity, level, or version")
        if min(self.hp, self.max_hp, self.attack, self.magic_attack, self.defense) < 0:
            raise ValueError("Character statistics cannot be negative")
        if self.hp > self.max_hp or self.experience < 0:
            raise ValueError("Character HP or experience is invalid")

    @property
    def computed_level(self) -> int:
        """@brief 从累计经验计算真实等级 / Compute the authoritative level from experience.

        @return 经验对应等级 / Level represented by experience.
        """

        return level_from_experience(self.experience)

    @property
    def experience_progress(self) -> tuple[int, int]:
        """@brief 返回当前等级经验进度 / Return current-level experience progress.

        @return ``(本级已获, 本级所需)`` / ``(earned in level, required in level)``.
        """

        level = self.computed_level
        previous = experience_threshold(level - 1)
        following = experience_threshold(level)
        return self.experience - previous, following - previous

    def set_battle_allowance(self, allow: bool) -> Character:
        """@brief 更新被挑战开关 / Update the challenge allowance.

        @param allow 新开关 / New setting.
        @return 版本递增角色 / Version-incremented character.
        """

        return replace(self, allow_battle=allow, version=self.version + 1)

    def heal(self) -> Character:
        """@brief 将生命值恢复到上限 / Restore HP to maximum.

        @return 版本递增角色 / Version-incremented character.
        """

        return replace(self, hp=self.max_hp, version=self.version + 1)

    def with_hp(self, hp: int) -> Character:
        """@brief 保存战后生命值 / Persist post-battle HP.

        @param hp 新生命值 / New HP.
        @return 版本递增角色 / Version-incremented character.
        """

        if hp < 0 or hp > self.max_hp:
            raise ValueError("Post-battle HP is out of range")
        return replace(self, hp=hp, version=self.version + 1)

    def gain_experience(self, amount: int) -> tuple[Character, LevelUp | None]:
        """@brief 增加经验并应用旧成长规则 / Add experience and apply legacy growth rules.

        @param amount 正经验值 / Positive experience amount.
        @return 新角色与可选升级事件 / New character and optional level-up event.
        """

        if amount <= 0:
            raise ValueError("Experience gain must be positive")
        new_experience = self.experience + amount
        new_level = level_from_experience(new_experience)
        level_difference = max(0, new_level - self.level)
        if level_difference == 0:
            return (
                replace(
                    self,
                    experience=new_experience,
                    level=new_level,
                    version=self.version + 1,
                ),
                None,
            )
        max_hp = self.max_hp + 5 * level_difference
        upgraded = replace(
            self,
            experience=new_experience,
            level=new_level,
            hp=max_hp,
            max_hp=max_hp,
            attack=self.attack + level_difference,
            defense=self.defense + level_difference,
            version=self.version + 1,
        )
        return upgraded, LevelUp(
            old_level=self.level,
            new_level=new_level,
            hp_increase=5 * level_difference,
            attack_increase=level_difference,
            defense_increase=level_difference,
        )


@dataclass(frozen=True, slots=True)
class LevelUp:
    """@brief 角色升级事件 / Character level-up event.

    @param old_level 原等级 / Previous level.
    @param new_level 新等级 / New level.
    @param hp_increase 最大生命成长 / Maximum HP growth.
    @param attack_increase 攻击成长 / Attack growth.
    @param defense_increase 防御成长 / Defense growth.
    """

    old_level: int
    new_level: int
    hp_increase: int
    attack_increase: int
    defense_increase: int


@dataclass(frozen=True, slots=True)
class Combatant:
    """@brief 战斗显示身份与角色 / Combat display identity and character.

    @param name 展示名 / Display name.
    @param character 角色聚合 / Character aggregate.
    """

    name: str
    character: Character

    def __post_init__(self) -> None:
        """@brief 校验展示名 / Validate the display name.

        @return None / None.
        """

        if not self.name.strip():
            raise ValueError("Combatant name cannot be empty")


@dataclass(frozen=True, slots=True)
class Monster:
    """@brief 可挑战怪物定义 / Challengeable monster definition.

    @param monster_id 稳定 ID / Stable ID.
    @param name 中文名称 / Chinese name.
    @param level 等级 / Level.
    @param hp 生命值 / HP.
    @param attack 攻击力 / Attack.
    @param defense 防御力 / Defense.
    @param experience_reward 经验奖励 / Experience reward.
    @param coin_reward 金币奖励 / Coin reward.
    @param description 描述 / Description.
    """

    monster_id: str
    name: str
    level: int
    hp: int
    attack: int
    defense: int
    experience_reward: int
    coin_reward: int
    description: str

    def __post_init__(self) -> None:
        """@brief 校验怪物配置 / Validate monster configuration.

        @return None / None.
        """

        if not self.monster_id or not self.name:
            raise ValueError("Monster identity and name are required")
        if (
            min(
                self.level,
                self.hp,
                self.attack,
                self.defense,
                self.experience_reward,
                self.coin_reward,
            )
            < 0
        ):
            raise ValueError("Monster statistics cannot be negative")


MONSTERS: Final[Mapping[str, Monster]] = MappingProxyType(
    {
        "goblin": Monster(
            "goblin",
            "哥布林",
            1,
            8,
            2,
            1,
            15,
            2,
            "一个弱小但狡猾的生物，常在森林中出没。",
        ),
        "wolf": Monster(
            "wolf", "野狼", 1, 5, 3, 1, 15, 3, "凶猛的野兽，群居生活，攻击力较强。"
        ),
        "skeleton": Monster(
            "skeleton",
            "骷髅兵",
            2,
            10,
            3,
            2,
            25,
            4,
            "被黑魔法复活的骸骨，手持生锈的武器。",
        ),
    }
)
"""@brief 与旧版本一致的怪物目录 / Monster catalog matching the legacy version."""


class BattleResult(StrEnum):
    """@brief 战斗终局分类 / Battle terminal classification."""

    WIN = "win"
    LOSE = "lose"
    DRAW = "draw"


@dataclass(frozen=True, slots=True)
class BattleTurn:
    """@brief 单次攻击日志 / One attack-log entry.

    @param number 攻击动作序号 / Attack-action number.
    @param attacker 攻击者展示名 / Attacker display name.
    @param defender 防守者展示名 / Defender display name.
    @param damage 伤害 / Damage.
    @param remaining_hp 防守者剩余生命 / Defender remaining HP.
    """

    number: int
    attacker: str
    defender: str
    damage: int
    remaining_hp: int


@dataclass(frozen=True, slots=True)
class PlayerBattle:
    """@brief PVP 纯战斗结果 / Pure PVP battle result.

    @param attacker_id 挑战者 ID / Attacker ID.
    @param defender_id 被挑战者 ID / Defender ID.
    @param attacker_hp 挑战者终局 HP / Attacker terminal HP.
    @param defender_hp 被挑战者终局 HP / Defender terminal HP.
    @param winner_id 胜者；平局为 None / Winner, or None on a draw.
    @param loser_id 负者；平局为 None / Loser, or None on a draw.
    @param turns 攻击日志 / Attack log.
    """

    attacker_id: int
    defender_id: int
    attacker_hp: int
    defender_hp: int
    winner_id: int | None
    loser_id: int | None
    turns: tuple[BattleTurn, ...]

    @property
    def is_draw(self) -> bool:
        """@brief 判断是否平局 / Return whether the battle is a draw.

        @return 没有胜者时为 True / True when no winner exists.
        """

        return self.winner_id is None


def fight_players(attacker: Combatant, defender: Combatant) -> PlayerBattle:
    """@brief 执行被挑战者先攻的旧 PVP / Run legacy defender-first PVP.

    @param attacker 挑战者 / Challenger.
    @param defender 被挑战者 / Defender.
    @return 至多二十次攻击的确定性结果 / Deterministic result of at most twenty attacks.
    """

    attacker_hp = attacker.character.hp
    defender_hp = defender.character.hp
    current_is_defender = True
    turns: list[BattleTurn] = []
    for number in range(1, MAX_BATTLE_TURNS + 1):
        if attacker_hp <= 0 or defender_hp <= 0:
            break
        if current_is_defender:
            damage = physical_damage(
                defender.character.attack, attacker.character.defense
            )
            attacker_hp = max(0, attacker_hp - damage)
            turns.append(
                BattleTurn(number, defender.name, attacker.name, damage, attacker_hp)
            )
        else:
            damage = physical_damage(
                attacker.character.attack, defender.character.defense
            )
            defender_hp = max(0, defender_hp - damage)
            turns.append(
                BattleTurn(number, attacker.name, defender.name, damage, defender_hp)
            )
        current_is_defender = not current_is_defender
    winner_id: int | None
    loser_id: int | None
    if defender_hp <= 0:
        winner_id, loser_id = attacker.character.user_id, defender.character.user_id
    elif attacker_hp <= 0:
        winner_id, loser_id = defender.character.user_id, attacker.character.user_id
    else:
        winner_id = loser_id = None
    return PlayerBattle(
        attacker_id=attacker.character.user_id,
        defender_id=defender.character.user_id,
        attacker_hp=attacker_hp,
        defender_hp=defender_hp,
        winner_id=winner_id,
        loser_id=loser_id,
        turns=tuple(turns),
    )


@dataclass(frozen=True, slots=True)
class MonsterBattle:
    """@brief PVE 纯战斗结果 / Pure PVE battle result.

    @param player_hp 玩家终局 HP / Player terminal HP.
    @param monster_hp 怪物终局 HP / Monster terminal HP.
    @param result 胜负分类 / Outcome classification.
    @param turns 攻击日志 / Attack log.
    """

    player_hp: int
    monster_hp: int
    result: BattleResult
    turns: tuple[BattleTurn, ...]


def fight_monster(player: Combatant, monster: Monster) -> MonsterBattle:
    """@brief 执行玩家先攻的旧 PVE / Run legacy player-first PVE.

    @param player 玩家 / Player.
    @param monster 怪物 / Monster.
    @return 至多二十次攻击的确定性结果 / Deterministic result of at most twenty attacks.
    """

    player_hp = player.character.hp
    monster_hp = monster.hp
    player_turn = True
    turns: list[BattleTurn] = []
    for number in range(1, MAX_BATTLE_TURNS + 1):
        if player_hp <= 0 or monster_hp <= 0:
            break
        if player_turn:
            damage = physical_damage(player.character.attack, monster.defense)
            monster_hp = max(0, monster_hp - damage)
            turns.append(
                BattleTurn(number, player.name, monster.name, damage, monster_hp)
            )
        else:
            damage = physical_damage(monster.attack, player.character.defense)
            player_hp = max(0, player_hp - damage)
            turns.append(
                BattleTurn(number, monster.name, player.name, damage, player_hp)
            )
        player_turn = not player_turn
    if player_hp <= 0 and monster_hp <= 0:
        result = BattleResult.DRAW
    elif player_hp <= 0:
        result = BattleResult.LOSE
    elif monster_hp <= 0:
        result = BattleResult.WIN
    else:
        result = BattleResult.DRAW
    return MonsterBattle(player_hp, monster_hp, result, tuple(turns))


class EquipmentSlot(StrEnum):
    """@brief RPG 装备槽 / RPG equipment slots."""

    WEAPON = "weapon"
    OFFHAND = "offhand"
    ARMOR = "armor"
    TREASURE1 = "treasure1"
    TREASURE2 = "treasure2"


EQUIPMENT_SLOT_NAMES: Final[Mapping[EquipmentSlot, str]] = MappingProxyType(
    {
        EquipmentSlot.WEAPON: "武器",
        EquipmentSlot.OFFHAND: "副武器",
        EquipmentSlot.ARMOR: "护甲",
        EquipmentSlot.TREASURE1: "宝物1",
        EquipmentSlot.TREASURE2: "宝物2",
    }
)
"""@brief 装备槽中文名 / Chinese equipment-slot names."""


@dataclass(frozen=True, slots=True)
class Equipment:
    """@brief RPG 装备定义 / RPG equipment definition.

    @param equipment_id 装备 ID / Equipment ID.
    @param name 名称 / Name.
    @param slot 槽位 / Slot.
    @param attack_bonus 攻击加成 / Attack bonus.
    @param defense_bonus 防御加成 / Defense bonus.
    @param hp_bonus 生命加成 / HP bonus.
    @param magic_attack_bonus 魔攻加成 / Magic-attack bonus.
    @param description 描述 / Description.
    @param price 价格 / Price.
    @param rarity 稀有度 / Rarity.
    """

    equipment_id: int
    name: str
    slot: EquipmentSlot
    attack_bonus: int
    defense_bonus: int
    hp_bonus: int
    magic_attack_bonus: int
    description: str | None
    price: int
    rarity: int


@dataclass(frozen=True, slots=True)
class EquipmentLoadout:
    """@brief 玩家五槽装备快照 / Player five-slot equipment snapshot.

    @param user_id 玩家 ID / Player ID.
    @param slots 槽位到装备的不可变映射元组 / Immutable slot-to-equipment pairs.
    @param version OCC 版本 / OCC version.
    """

    user_id: int
    slots: tuple[tuple[EquipmentSlot, Equipment | None], ...]
    version: int = 0

    @classmethod
    def empty(cls, user_id: int) -> EquipmentLoadout:
        """@brief 创建空装备快照 / Create an empty loadout.

        @param user_id 玩家 ID / Player ID.
        @return 五槽空快照 / Empty five-slot snapshot.
        """

        return cls(user_id, tuple((slot, None) for slot in EquipmentSlot), 0)

    def item_at(self, slot: EquipmentSlot) -> Equipment | None:
        """@brief 读取槽位装备 / Read equipment in a slot.

        @param slot 槽位 / Slot.
        @return 装备或 None / Equipment or None.
        """

        return dict(self.slots).get(slot)

    @property
    def bonuses(self) -> tuple[int, int, int, int]:
        """@brief 汇总装备属性 / Sum equipment bonuses.

        @return ``(ATK, DEF, HP, MATK)`` / ``(ATK, DEF, HP, MATK)``.
        """

        equipped = tuple(item for _, item in self.slots if item is not None)
        return (
            sum(item.attack_bonus for item in equipped),
            sum(item.defense_bonus for item in equipped),
            sum(item.hp_bonus for item in equipped),
            sum(item.magic_attack_bonus for item in equipped),
        )


class ItemType(StrEnum):
    """@brief RPG 道具类型 / RPG item types."""

    CONSUMABLE = "consumable"
    MATERIAL = "material"
    QUEST = "quest"


ITEM_TYPE_NAMES: Final[Mapping[ItemType, str]] = MappingProxyType(
    {
        ItemType.CONSUMABLE: "消耗品",
        ItemType.MATERIAL: "材料",
        ItemType.QUEST: "任务物品",
    }
)
"""@brief 道具类型中文名 / Chinese item-type names."""


@dataclass(frozen=True, slots=True)
class Item:
    """@brief RPG 道具定义 / RPG item definition.

    @param item_id 道具 ID / Item ID.
    @param name 名称 / Name.
    @param item_type 类型 / Type.
    @param effect 旧自由文本效果 / Legacy free-text effect.
    @param description 描述 / Description.
    @param price 价格 / Price.
    @param use_limit 使用上限 / Use limit.
    """

    item_id: int
    name: str
    item_type: ItemType
    effect: str | None
    description: str | None
    price: int
    use_limit: int


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """@brief 背包中的一类道具 / One item kind in inventory.

    @param item 道具定义 / Item definition.
    @param quantity 正数量 / Positive quantity.
    @param version OCC 版本 / OCC version.
    """

    item: Item
    quantity: int
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 校验数量 / Validate quantity.

        @return None / None.
        """

        if self.quantity <= 0 or self.version < 0:
            raise ValueError("Inventory quantities must be positive")


__all__ = [
    "BattleResult",
    "BattleTurn",
    "Character",
    "Combatant",
    "EQUIPMENT_SLOT_NAMES",
    "Equipment",
    "EquipmentLoadout",
    "EquipmentSlot",
    "INVENTORY_CAPACITY",
    "ITEM_TYPE_NAMES",
    "InventoryEntry",
    "Item",
    "ItemType",
    "LevelUp",
    "MAX_BATTLE_TURNS",
    "MAX_LEVEL",
    "MONSTERS",
    "Monster",
    "MonsterBattle",
    "PlayerBattle",
    "experience_gain",
    "experience_threshold",
    "fight_monster",
    "fight_players",
    "level_from_experience",
    "magical_damage",
    "physical_damage",
]
