"""@brief 充值应用模型与端口 / Top-up application models and port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class TopUpAccountStatus:
    """@brief 充值账户状态 / Top-up account status.

    @param exists 账户是否存在 / Whether the account exists.
    @param name 展示名 / Display name.
    @param blocked_until 禁用截止时间 / Block deadline.
    """

    exists: bool
    name: str | None = None
    blocked_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApproveTopUp:
    """@brief 管理员确认充值命令 / Administrator top-up approval.

    @param user_id 目标用户 ID / Target user ID.
    @param coins 付费金币 / Paid coins.
    @param idempotency_key 来源 callback Update 键 / Callback-Update-derived key.
    """

    user_id: int
    coins: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TopUpResult:
    """@brief 充值处理结果 / Top-up processing result.

    @param code 结果代码 / Result code.
    @param name 用户展示名 / User display name.
    @param coins 已发金币 / Credited coins.
    """

    code: EconomyCode
    name: str | None = None
    coins: int = 0


class TopUpOperations(Protocol):
    """@brief 充值持久化能力端口 / Top-up persistence capability port."""

    async def topup_status(self, user_id: int) -> TopUpAccountStatus:
        """@brief 读取充值账户状态 / Read top-up account status.

        @param user_id 用户 ID / User ID.
        @return 充值状态 / Top-up status.
        """

        ...

    async def approve_topup(self, command: ApproveTopUp) -> TopUpResult:
        """@brief 幂等发放付费金币 / Idempotently credit paid coins.

        @param command 充值命令 / Top-up command.
        @return 充值结果 / Top-up result.
        """

        ...

    async def block_recharge(self, user_id: int, until: datetime) -> TopUpResult:
        """@brief 禁用用户充值入口 / Block a user's recharge entry.

        @param user_id 用户 ID / User ID.
        @param until 截止时间 / Deadline.
        @return 处理结果 / Processing result.
        """

        ...


__all__ = ["ApproveTopUp", "TopUpAccountStatus", "TopUpOperations", "TopUpResult"]
