"""@brief 经济应用层共享类型 / Shared economy application-layer types."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class EconomyCode(StrEnum):
    """@brief 经济用例结果代码 / Economy use-case result code."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"
    ALREADY_CLAIMED = "already_claimed"
    INSUFFICIENT_COINS = "insufficient_coins"
    NOT_FOUND = "not_found"
    ALREADY_USED = "already_used"
    INVALID = "invalid"
    ALREADY_BOUND = "already_bound"
    REFERRER_NOT_FOUND = "referrer_not_found"
    SELF_REFERRAL = "self_referral"
    PERMISSION_PREREQUISITE = "permission_prerequisite"
    ALREADY_OWNED = "already_owned"
    DAILY_LIMIT = "daily_limit"
    SELF_TRANSFER = "self_transfer"


class AccountLookup(Protocol):
    """@brief 账户存在性查询端口 / Account-existence lookup port."""

    async def account_exists(self, user_id: int) -> bool:
        """@brief 检查账户是否存在 / Check whether an account exists.

        @param user_id 用户 ID / User ID.
        @return 存在为 True / True when present.
        """

        ...


__all__ = ["AccountLookup", "EconomyCode"]
