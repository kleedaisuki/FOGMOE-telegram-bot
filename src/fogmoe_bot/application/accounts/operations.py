"""@brief Identity/account 账户用例与端口 / Identity/account use cases and ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fogmoe_bot.domain.accounts.plan import AccountPlan

ACCOUNT_SERVICE_DATA_KEY = "fogmoe.account.service"
"""@brief runtime capability 中账户服务键 / Account-service key in runtime capabilities."""


class AccountCode(StrEnum):
    """@brief 账户用例结果代码 / Account use-case result code."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """@brief 对外展示的账户快照 / Account snapshot exposed to the user.

    @param user_id 用户 ID / User ID.
    @param username Telegram username / Telegram username.
    @param permission 权限等级 / Permission level.
    @param plan 套餐 / Plan.
    @param free_coins 免费金币 / Free coins.
    @param paid_coins 付费金币 / Paid coins.
    """

    user_id: int
    username: str
    permission: int
    plan: AccountPlan
    free_coins: int
    paid_coins: int

    @property
    def total_coins(self) -> int:
        """@brief 返回总金币 / Return total coins.

        @return 免费与付费之和 / Sum of free and paid coins.
        """

        return self.free_coins + self.paid_coins


@dataclass(frozen=True, slots=True)
class RegisterAccount:
    """@brief 注册或刷新 Telegram 账户命令 / Command registering or refreshing a Telegram account.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param username 规范 username / Normalized username.
    @param initial_coins 新账户奖励 / New-account bonus.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    user_id: int
    username: str
    initial_coins: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AccountRegistrationResult:
    """@brief 注册结果 / Account-registration result.

    @param profile 首次命令提交时的稳定快照 / Stable snapshot at first command commit.
    @param replayed 是否回放 / Whether replayed.
    """

    profile: AccountProfile
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PersonalInfoCommand:
    """@brief 查看或更新个人提示信息 / Inspect or update personal prompt information.

    @param user_id 用户 ID / User ID.
    @param new_info 新值；None 仅查看 / New value; None means inspect only.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    user_id: int
    new_info: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PersonalInfoResult:
    """@brief 个人信息命令结果 / Personal-info command result.

    @param code 结果代码 / Result code.
    @param previous_info 命令前值 / Value before the command.
    @param current_info 命令后值 / Value after the command.
    @param updated 是否执行更新 / Whether an update was performed.
    @param replayed 是否回放 / Whether replayed.
    """

    code: AccountCode
    previous_info: str = ""
    current_info: str = ""
    updated: bool = False
    replayed: bool = False


class AccountOperations(Protocol):
    """@brief 账户原子持久化端口 / Atomic account-persistence port."""

    async def register(
        self,
        command: RegisterAccount,
    ) -> AccountRegistrationResult:
        """@brief 幂等注册并返回稳定快照 / Idempotently register and return a stable snapshot."""

        ...

    async def personal_info(self, command: PersonalInfoCommand) -> PersonalInfoResult:
        """@brief 幂等查看或更新个人信息 / Idempotently inspect or update personal information."""

        ...


class AccountService:
    """@brief 账户用例 facade / Account use-case facade."""

    def __init__(
        self,
        operations: AccountOperations,
        *,
        initial_coins: int,
    ) -> None:
        """@brief 注入原子端口与产品配置 / Inject atomic operations and product configuration.

        @param operations 账户持久化端口 / Account-persistence port.
        @param initial_coins 新账户奖励 / New-account bonus.
        @raise ValueError 配置非法 / Invalid configuration.
        """

        if initial_coins < 0:
            raise ValueError("initial_coins cannot be negative")
        self._operations = operations
        self._initial_coins = initial_coins

    async def register(
        self,
        user_id: int,
        username: str,
        *,
        idempotency_key: str,
    ) -> AccountRegistrationResult:
        """@brief 注册或刷新 Telegram 账户 / Register or refresh a Telegram account.

        @param user_id Telegram 用户 ID / Telegram user ID.
        @param username Telegram username / Telegram username.
        @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
        @return 稳定账户快照 / Stable account snapshot.
        """

        _validate_identity(user_id, idempotency_key)
        normalized = username.strip().removeprefix("@")
        if not normalized or len(normalized) > 64:
            raise ValueError("username must contain 1-64 characters")
        return await self._operations.register(
            RegisterAccount(
                user_id=user_id,
                username=normalized,
                initial_coins=self._initial_coins,
                idempotency_key=idempotency_key,
            )
        )

    async def personal_info(
        self,
        user_id: int,
        new_info: str | None,
        *,
        idempotency_key: str,
    ) -> PersonalInfoResult:
        """@brief 查看、清空或更新个人信息 / Inspect, clear, or update personal information.

        @param user_id 用户 ID / User ID.
        @param new_info 新值；None 仅查看 / New value; None means inspect only.
        @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
        @return 稳定结果 / Stable result.
        """

        _validate_identity(user_id, idempotency_key)
        normalized = new_info.strip() if new_info is not None else None
        if normalized is not None and len(normalized) > 500:
            raise ValueError("personal info cannot exceed 500 characters")
        return await self._operations.personal_info(
            PersonalInfoCommand(
                user_id=user_id,
                new_info=normalized,
                idempotency_key=idempotency_key,
            )
        )


def _validate_identity(user_id: int, idempotency_key: str) -> None:
    """@brief 校验账户命令 identity / Validate account-command identity.

    @param user_id 用户 ID / User ID.
    @param idempotency_key 幂等键 / Idempotency key.
    @return None / None.
    """

    if user_id <= 0:
        raise ValueError("user_id must be positive")
    if not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ValueError("idempotency_key must contain 1-200 characters")


__all__ = [
    "ACCOUNT_SERVICE_DATA_KEY",
    "AccountCode",
    "AccountOperations",
    "AccountProfile",
    "AccountRegistrationResult",
    "AccountService",
    "PersonalInfoCommand",
    "PersonalInfoResult",
    "RegisterAccount",
]
