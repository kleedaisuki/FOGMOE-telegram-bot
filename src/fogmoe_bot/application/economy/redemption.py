"""@brief 卡密兑换应用模型与端口 / Redemption-code models and port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class RedeemCodeCommand:
    """@brief 兑换卡密命令 / Redemption-code command.

    @param user_id 用户 ID / User ID.
    @param code 规范化 UUID 卡密 / Normalized UUID code.
    @param redeemed_at 兑换时间 / Redemption time.
    @param idempotency_key 幂等键 / Idempotency key.
    """

    user_id: int
    code: str
    redeemed_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RedeemCodeResult:
    """@brief 卡密兑换结果 / Redemption-code result.

    @param code 结果代码 / Result code.
    @param amount 入账金币 / Credited coins.
    @param balance 入账后总余额 / Post-credit total balance.
    @param used_by 已使用者 / Existing redeemer.
    @param used_at 已使用时间 / Existing redemption time.
    """

    code: EconomyCode
    amount: int = 0
    balance: int = 0
    used_by: int | None = None
    used_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CreateCodesCommand:
    """@brief 创建卡密命令 / Create-redemption-codes command.

    @param codes 事务外生成的 UUID / UUIDs generated outside the transaction.
    @param amount 每枚卡密金币 / Coins per code.
    """

    codes: tuple[str, ...]
    amount: int


class RedemptionOperations(Protocol):
    """@brief 卡密持久化能力端口 / Redemption-code persistence capability port."""

    async def redeem(self, command: RedeemCodeCommand) -> RedeemCodeResult:
        """@brief 原子兑换卡密 / Atomically redeem a code.

        @param command 兑换命令 / Redemption command.
        @return 兑换结果 / Redemption result.
        """

        ...

    async def create_codes(self, command: CreateCodesCommand) -> tuple[str, ...]:
        """@brief 原子创建卡密 / Atomically create redemption codes.

        @param command 创建命令 / Creation command.
        @return 已创建卡密 / Created codes.
        """

        ...


__all__ = [
    "CreateCodesCommand",
    "RedeemCodeCommand",
    "RedeemCodeResult",
    "RedemptionOperations",
]
