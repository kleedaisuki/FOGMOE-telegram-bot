"""@brief 不可变双重记账模型 / Immutable double-entry ledger model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from .money import SystemAccountKind, TokenAmount, TokenBucket


class AccountScope(StrEnum):
    """@brief 账本账户所有者范围 / Ledger-account owner scope."""

    USER = "user"
    """@brief 个人账户 / Personal account."""

    GROUP = "group"
    """@brief 群组小镇账户 / Group-town account."""

    SYSTEM = "system"
    """@brief 银行系统账户 / Bank system account."""


@dataclass(frozen=True, slots=True, order=True)
class LedgerAccount:
    """@brief 账本账户逻辑标识 / Logical ledger-account identity.

    @param scope 所有者范围 / Owner scope.
    @param owner_id 用户或群组标识；系统账户为 None / User or group identity; None for system accounts.
    @param bucket 用户账户的钱包类别 / Wallet bucket for user accounts.
    @param system_kind 系统账户类型 / System-account kind.
    """

    scope: AccountScope
    """@brief 所有者范围 / Owner scope."""

    owner_id: int | None = None
    """@brief 用户或群组稳定标识 / Stable user or group identity."""

    bucket: TokenBucket | None = None
    """@brief 用户钱包类别 / User wallet bucket."""

    system_kind: SystemAccountKind | None = None
    """@brief 银行系统账户类型 / Bank system-account kind."""

    def __post_init__(self) -> None:
        """@brief 验证账户形状 / Validate account shape.

        @return None / None.
        @raise ValueError 标识和账户范围不匹配时抛出 / Raised when identity and scope do not match.
        """

        if self.scope is AccountScope.USER:
            if self.owner_id is None or self.owner_id <= 0 or self.bucket is None:
                raise ValueError(
                    "A user account needs a positive owner and wallet bucket"
                )
            if self.system_kind is not None:
                raise ValueError("A user account cannot have a system kind")
            return
        if self.scope is AccountScope.GROUP:
            if self.owner_id is None or self.owner_id == 0:
                raise ValueError("A group account needs a non-zero group identity")
            if (
                self.bucket is not None
                or self.system_kind is not SystemAccountKind.GROUP_TREASURY
            ):
                raise ValueError("A group account must be its group treasury")
            return
        if self.scope is AccountScope.SYSTEM:
            if (
                self.owner_id is not None
                or self.bucket is not None
                or self.system_kind is None
            ):
                raise ValueError("A system account only needs a system kind")
            return
        raise AssertionError("Unhandled ledger account scope")

    @classmethod
    def user(cls, user_id: int, bucket: TokenBucket) -> LedgerAccount:
        """@brief 构造用户钱包账户 / Construct a user-wallet account.

        @param user_id Telegram 用户稳定标识 / Stable Telegram user identity.
        @param bucket 钱包类别 / Wallet bucket.
        @return 用户账本账户 / User ledger account.
        """

        return cls(AccountScope.USER, owner_id=user_id, bucket=bucket)

    @classmethod
    def group_treasury(cls, chat_id: int) -> LedgerAccount:
        """@brief 构造群组金库账户 / Construct a group-treasury account.

        @param chat_id Telegram 群组稳定标识 / Stable Telegram group identity.
        @return 群组金库账户 / Group-treasury account.
        """

        return cls(
            AccountScope.GROUP,
            owner_id=chat_id,
            system_kind=SystemAccountKind.GROUP_TREASURY,
        )

    @classmethod
    def system(cls, kind: SystemAccountKind) -> LedgerAccount:
        """@brief 构造银行系统账户 / Construct a bank system account.

        @param kind 系统账户类型 / System-account kind.
        @return 系统账本账户 / System ledger account.
        """

        if kind is SystemAccountKind.GROUP_TREASURY:
            raise ValueError("Group treasury must use the group account scope")
        return cls(AccountScope.SYSTEM, system_kind=kind)


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """@brief 单个账本过账行 / One ledger posting line.

    ``delta`` 对目标账户的余额影响：正数是贷记，负数是借记。
    ``delta`` represents the target account's balance change: positive credits, negative debits.

    @param account 目标账户 / Target account.
    @param delta 有符号金额 / Signed amount.
    """

    account: LedgerAccount
    """@brief 被影响账户 / Affected account."""

    delta: int
    """@brief 有符号金币变化 / Signed token change."""

    def __post_init__(self) -> None:
        """@brief 验证过账金额 / Validate the posting amount.

        @return None / None.
        @raise TypeError 金额不是严格整数时抛出 / Raised when the amount is not a strict integer.
        @raise ValueError 金额为零时抛出 / Raised when the amount is zero.
        """

        if isinstance(self.delta, bool) or not isinstance(self.delta, int):
            raise TypeError("Ledger posting delta must be an integer")
        if self.delta == 0:
            raise ValueError("Ledger posting delta cannot be zero")


class LedgerReason(StrEnum):
    """@brief 可审计的资金移动原因 / Auditable fund-movement reason."""

    BANK_ISSUANCE = "bank_issuance"
    """@brief 管理员批准的代币发行 / Administrator-approved token issuance."""

    MIGRATION_OPENING = "migration_opening"
    """@brief 历史余额开账导入 / Historical-balance opening import."""

    BANK_BURN = "bank_burn"
    """@brief 永久金币销毁 / Permanent token burn."""

    USER_TRANSFER = "user_transfer"
    """@brief 用户间转账 / User-to-user transfer."""

    GROUP_CONTRIBUTION = "group_contribution"
    """@brief 向群组小镇金库贡献 / Contribution to a group-town treasury."""

    ACTIVITY_STAKE = "activity_stake"
    """@brief 活动下注或入场托管 / Activity stake or entry escrow."""

    ACTIVITY_PAYOUT = "activity_payout"
    """@brief 活动派彩 / Activity payout."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """@brief 一个平衡且不可变的账本分录 / One balanced immutable ledger entry.

    @param entry_id 分录稳定标识 / Stable entry identity.
    @param idempotency_key 调用者提供的全局幂等键 / Caller-provided global idempotency key.
    @param reason 资金变化原因 / Fund-movement reason.
    @param postings 借贷过账行 / Debit and credit posting lines.
    @param created_at 创建时刻 / Creation instant.
    @param actor_id 发起用户；系统操作为 None / Initiating user; None for system operations.
    @param metadata 审计元数据 / Audit metadata.
    """

    entry_id: UUID
    """@brief 分录标识 / Entry identity."""

    idempotency_key: str
    """@brief 全局幂等键 / Global idempotency key."""

    reason: LedgerReason
    """@brief 业务原因 / Business reason."""

    postings: tuple[LedgerPosting, ...]
    """@brief 平衡过账行 / Balanced posting lines."""

    created_at: datetime
    """@brief 时区感知创建时刻 / Timezone-aware creation instant."""

    actor_id: int | None = None
    """@brief 可选操作者 / Optional actor."""

    metadata: Mapping[str, str | int | bool] = MappingProxyType({})
    """@brief 小型审计元数据 / Small audit metadata."""

    def __post_init__(self) -> None:
        """@brief 验证双重记账不变量 / Validate double-entry invariants.

        @return None / None.
        @raise ValueError 分录不平衡、重复账户或元数据非法时抛出 /
            Raised when the entry is unbalanced, repeats an account, or has invalid metadata.
        """

        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:
            raise ValueError("Ledger idempotency key must contain 1-200 characters")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Ledger entry time must be timezone-aware")
        if self.actor_id is not None and self.actor_id <= 0:
            raise ValueError("Ledger actor must be positive when present")
        if len(self.postings) < 2:
            raise ValueError("A ledger entry needs at least two postings")
        if sum(posting.delta for posting in self.postings) != 0:
            raise ValueError("Ledger entry must balance to zero")
        accounts = tuple(posting.account for posting in self.postings)
        if len(set(accounts)) != len(accounts):
            raise ValueError("A ledger entry cannot repeat an account")
        normalized_metadata = dict(self.metadata)
        if any(not key.strip() for key in normalized_metadata):
            raise ValueError("Ledger metadata keys cannot be blank")
        object.__setattr__(self, "metadata", MappingProxyType(normalized_metadata))

    @classmethod
    def transfer(
        cls,
        *,
        entry_id: UUID,
        idempotency_key: str,
        reason: LedgerReason,
        source: LedgerAccount,
        destination: LedgerAccount,
        amount: TokenAmount,
        created_at: datetime,
        actor_id: int | None = None,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> LedgerEntry:
        """@brief 构造标准双边转账分录 / Construct a standard two-party transfer entry.

        @param entry_id 分录标识 / Entry identity.
        @param idempotency_key 幂等键 / Idempotency key.
        @param reason 业务原因 / Business reason.
        @param source 扣款账户 / Debited account.
        @param destination 入账账户 / Credited account.
        @param amount 正数金额 / Positive token amount.
        @param created_at 创建时刻 / Creation instant.
        @param actor_id 可选操作者 / Optional actor.
        @param metadata 可选审计元数据 / Optional audit metadata.
        @return 已平衡账本分录 / Balanced ledger entry.
        @raise ValueError 源和目标相同时抛出 / Raised when source and destination match.
        """

        if source == destination:
            raise ValueError("Ledger transfer source and destination must differ")
        return cls(
            entry_id=entry_id,
            idempotency_key=idempotency_key,
            reason=reason,
            postings=(
                LedgerPosting(source, -amount.value),
                LedgerPosting(destination, amount.value),
            ),
            created_at=created_at,
            actor_id=actor_id,
            metadata={} if metadata is None else metadata,
        )
