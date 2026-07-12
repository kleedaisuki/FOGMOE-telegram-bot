"""@brief RPG 应用层共享结果语义 / Shared RPG application-result semantics."""

from enum import StrEnum


class RpgCode(StrEnum):
    """@brief RPG 用例的穷尽结果代码 / Exhaustive RPG use-case result codes."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT_COINS = "insufficient_coins"
    NOT_FOUND = "not_found"
    NO_CHARACTER = "no_character"
    DEAD = "dead"
    TARGET_NOT_FOUND = "target_not_found"
    SELF_TARGET = "self_target"
    TARGET_NO_CHARACTER = "target_no_character"
    TARGET_DISALLOWS = "target_disallows"
    TARGET_DEAD = "target_dead"
    COOLDOWN = "cooldown"
    ALREADY_FULL_HP = "already_full_hp"
    INVENTORY_FULL = "inventory_full"
    NOT_OWNED = "not_owned"
    INSUFFICIENT_QUANTITY = "insufficient_quantity"
    WRONG_ITEM_TYPE = "wrong_item_type"
    EMPTY_SLOT = "empty_slot"
