"""@brief 每日签到命令、结果与持久化端口 / Daily check-in commands, results, and persistence port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from fogmoe_bot.domain.economy.identity import EconomyAccountId

from .common import EconomyCode


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckInCommand:
    """@brief 领取一个业务日签到奖励的应用命令 / Application command claiming one business-day reward.

    @param account_id 已验证的 Economy 账户身份 / Validated Economy account identity.
    @param day 待领取业务日期 / Business date to claim.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    account_id: EconomyAccountId
    """@brief 已验证的 Economy 账户身份 / Validated Economy account identity."""

    day: date
    """@brief 待领取业务日期 / Business date to claim."""

    idempotency_key: str
    """@brief 来源 Update 幂等键 / Source-Update idempotency key."""

    def __post_init__(self) -> None:
        """@brief 验证应用消息边界 / Validate the application-message boundary.

        @return None / None.
        @raise TypeError 身份、日期或幂等键类型非法时抛出 /
            Raised when identity, date, or idempotency-key types are invalid.
        @raise ValueError 幂等键为空或过长时抛出 / Raised when the idempotency key is blank or too long.
        """

        if not isinstance(self.account_id, EconomyAccountId):
            raise TypeError("Check-in command requires EconomyAccountId")
        if type(self.day) is not date:
            raise TypeError("Check-in command day must be a date")
        if not isinstance(self.idempotency_key, str):
            raise TypeError("Check-in idempotency key must be a string")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:
            raise ValueError("Check-in idempotency key must contain 1-200 characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckInResult:
    """@brief 对外稳定的签到用例结果 / Stable outward result of the check-in use case.

    @param code 用例结果代码 / Use-case result code.
    @param consecutive_days 当前连续签到天数 / Current consecutive-day count.
    @param reward 本次实际发放金币 / Tokens actually granted by this invocation.
    @param replayed 是否来自已提交回执 / Whether restored from a committed receipt.
    """

    code: EconomyCode
    """@brief 用例结果代码 / Use-case result code."""

    consecutive_days: int = 0
    """@brief 当前连续签到天数 / Current consecutive-day count."""

    reward: int = 0
    """@brief 本次实际发放金币 / Tokens actually granted by this invocation."""

    replayed: bool = False
    """@brief 是否来自已提交回执 / Whether restored from a committed receipt."""

    def __post_init__(self) -> None:
        """@brief 拒绝无法由签到生命周期产生的结果形状 / Reject shapes impossible for the check-in lifecycle.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when field types are invalid.
        @raise ValueError 结果代码与数值形状不一致时抛出 /
            Raised when the result code and numeric shape are inconsistent.
        """

        if not isinstance(self.code, EconomyCode):
            raise TypeError("Check-in result code must be EconomyCode")
        for field, value in (
            ("consecutive days", self.consecutive_days),
            ("reward", self.reward),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Check-in result {field} must be an integer")
            if value < 0:
                raise ValueError(f"Check-in result {field} cannot be negative")
        if not isinstance(self.replayed, bool):
            raise TypeError("Check-in replay marker must be a boolean")
        if self.code is EconomyCode.SUCCESS:
            if self.consecutive_days <= 0 or not 1 <= self.reward <= 7:
                raise ValueError("Successful check-in must include its streak and reward")
            return
        if self.code is EconomyCode.ALREADY_CLAIMED:
            if self.consecutive_days <= 0 or self.reward != 0:
                raise ValueError("Repeated check-in must preserve the streak without a reward")
            return
        if self.code is EconomyCode.NOT_REGISTERED:
            if self.consecutive_days != 0 or self.reward != 0:
                raise ValueError("Unregistered check-in cannot include a streak or reward")
            return
        raise ValueError("Unsupported check-in result code")


class CheckInOperations(Protocol):
    """@brief 签到事务持久化端口 / Transactional persistence port for check-in."""

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 原子执行领域签到决策、账本发放与回执写入 /
        Atomically execute the domain decision, ledger grant, and receipt write.

        @param command 已验证签到命令 / Validated check-in command.
        @return 稳定且可回放的用例结果 / Stable replayable use-case result.
        """

        ...


__all__ = ["CheckInCommand", "CheckInOperations", "CheckInResult"]
