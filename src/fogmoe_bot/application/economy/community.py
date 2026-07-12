"""@brief 社区经济应用模型与端口 / Community-economy models and port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class GiftCommand:
    """@brief 用户间金币赠送命令 / User-to-user coin-gift command.

    @param sender_id 赠送者 ID / Sender ID.
    @param target_name 目标 Telegram username / Target Telegram username.
    @param amount 到账金额 / Credited amount.
    @param fee 销毁手续费 / Burned fee.
    @param business_date 每日次数日期 / Daily-limit date.
    @param daily_limit 每日次数上限 / Daily count limit.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    sender_id: int
    target_name: str
    amount: int
    fee: int
    business_date: date
    daily_limit: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class GiftResult:
    """@brief 用户赠送结果 / User-gift result.

    @param code 结果代码 / Result code.
    @param target_name 规范目标名 / Canonical target name.
    @param amount 到账金额 / Credited amount.
    @param fee 手续费 / Fee.
    @param available 赠送前可用余额 / Available balance before the gift.
    @param replayed 是否回放 / Whether replayed.
    """

    code: EconomyCode
    target_name: str | None = None
    amount: int = 0
    fee: int = 0
    available: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """@brief 金币排行榜条目 / Coin-leaderboard entry.

    @param name 用户名 / Username.
    @param coins 免费与付费金币总额 / Total free and paid coins.
    """

    name: str
    coins: int


@dataclass(frozen=True, slots=True)
class LeaderboardCommand:
    """@brief 请求稳定排行榜快照 / Request a stable leaderboard snapshot.

    @param requester_id 请求用户 / Requesting user.
    @param limit 最大条目 / Maximum entries.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    requester_id: int
    limit: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class LeaderboardResult:
    """@brief 排行榜快照结果 / Leaderboard-snapshot result.

    @param code 结果代码 / Result code.
    @param entries 冻结条目 / Frozen entries.
    @param replayed 是否回放 / Whether replayed.
    """

    code: EconomyCode
    entries: tuple[LeaderboardEntry, ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TaskClaimCommand:
    """@brief 已验证群成员身份的任务领取 / Task claim after membership verification.

    @param user_id 用户 ID / User ID.
    @param task_id 任务 ID / Task ID.
    @param reward 奖励金币 / Reward coins.
    @param idempotency_key 幂等键 / Idempotency key.
    """

    user_id: int
    task_id: int
    reward: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TaskClaimResult:
    """@brief 任务领取结果 / Task-claim result.

    @param code 结果代码 / Result code.
    @param reward 已发金币 / Granted coins.
    """

    code: EconomyCode
    reward: int = 0


class CommunityOperations(Protocol):
    """@brief 社区经济持久化能力端口 / Community-economy persistence capability port."""

    async def give(self, command: GiftCommand) -> GiftResult:
        """@brief 原子赠送金币 / Atomically gift coins.

        @param command 赠送命令 / Gift command.
        @return 赠送结果 / Gift result.
        """

        ...

    async def leaderboard(self, command: LeaderboardCommand) -> LeaderboardResult:
        """@brief 读取稳定金币排行榜 / Read a stable coin leaderboard.

        @param command 排行榜命令 / Leaderboard command.
        @return 稳定排行榜 / Stable leaderboard.
        """

        ...

    async def claim_task(self, command: TaskClaimCommand) -> TaskClaimResult:
        """@brief 原子领取任务 / Atomically claim a task.

        @param command 任务命令 / Task command.
        @return 任务结果 / Task result.
        """

        ...


def calculate_gift_fee(amount: int) -> int:
    """@brief 计算旧赠送手续费 / Calculate the legacy gift fee.

    @param amount 到账金额 / Credited amount.
    @return 1 枚不收费，其余向下取 20% 且至少 1 / Zero for one coin; otherwise floor 20% with a minimum of one.
    """

    if amount <= 0:
        raise ValueError("Gift amount must be positive")
    if amount == 1:
        return 0
    return max(1, amount // 5)


__all__ = [
    "CommunityOperations",
    "GiftCommand",
    "GiftResult",
    "LeaderboardCommand",
    "LeaderboardEntry",
    "LeaderboardResult",
    "TaskClaimCommand",
    "TaskClaimResult",
    "calculate_gift_fee",
]
