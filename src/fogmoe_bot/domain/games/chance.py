"""@brief 机会游戏的纯领域模型 / Pure domain models for games of chance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
import hashlib
import random
from types import MappingProxyType
from typing import Final
from uuid import UUID


class GameSessionKind(StrEnum):
    """@brief 持久化游戏会话种类 / Persisted game-session kind."""

    GAMBLE = "gamble"
    """@brief 五分钟多人奖池 / Five-minute multiplayer prize pool."""

    SICBO = "sicbo"
    """@brief 单人骰宝选择流程 / Single-player Sic Bo selection flow."""


class GameSessionStatus(StrEnum):
    """@brief 持久化游戏会话状态 / Persisted game-session status."""

    ACTIVE = "active"
    """@brief 可接受状态转移 / Accepting transitions."""

    SETTLED = "settled"
    """@brief 已完成原子结算 / Atomically settled."""

    CANCELLED = "cancelled"
    """@brief 已由玩家取消 / Cancelled by the player."""

    EXPIRED = "expired"
    """@brief 超时且没有经济结算 / Expired without an economic settlement."""


@dataclass(frozen=True, slots=True, order=True)
class GameSessionId:
    """@brief 游戏会话稳定身份 / Stable game-session identity.

    @param value UUID 值 / UUID value.
    """

    value: UUID

    def __str__(self) -> str:
        """@brief 返回数据库编码 / Return the database encoding.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True)
class GambleBet:
    """@brief 一笔多人奖池下注 / One multiplayer-pool bet.

    @param user_id 玩家 ID / Player ID.
    @param display_name 展示名 / Display name.
    @param amount 押注金币 / Wagered coins.
    """

    user_id: int
    display_name: str
    amount: int

    def __post_init__(self) -> None:
        """@brief 校验下注不变量 / Validate bet invariants.

        @return None / None.
        """

        if self.user_id <= 0 or self.amount <= 0:
            raise ValueError("A gamble bet requires a positive user and amount")
        if not self.display_name.strip():
            raise ValueError("A gamble bet requires a display name")


@dataclass(frozen=True, slots=True)
class GambleSession:
    """@brief 可恢复的多人奖池聚合 / Recoverable multiplayer-pool aggregate.

    @param session_id 会话 ID / Session ID.
    @param chat_id Telegram chat ID / Telegram chat ID.
    @param message_id Telegram message ID / Telegram message ID.
    @param closes_at 开奖截止时间 / Settlement deadline.
    @param bets 已接受下注 / Accepted bets.
    @param status 会话状态 / Session status.
    @param version OCC 版本 / OCC version.
    """

    session_id: GameSessionId
    chat_id: int
    message_id: int
    closes_at: datetime
    bets: tuple[GambleBet, ...] = ()
    status: GameSessionStatus = GameSessionStatus.ACTIVE
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 校验聚合不变量 / Validate aggregate invariants.

        @return None / None.
        """

        if self.message_id <= 0 or self.version < 0:
            raise ValueError("Invalid gamble message identity or version")
        users = [bet.user_id for bet in self.bets]
        if len(users) != len(set(users)):
            raise ValueError("A user may bet only once in a gamble session")

    @property
    def prize(self) -> int:
        """@brief 返回奖池总额 / Return the total prize pool.

        @return 所有下注之和 / Sum of all wagers.
        """

        return sum(bet.amount for bet in self.bets)

    def place(self, bet: GambleBet, *, now: datetime) -> GambleSession:
        """@brief 接受一笔合法下注 / Accept one valid wager.

        @param bet 新下注 / New bet.
        @param now 当前时间 / Current time.
        @return 版本递增的新聚合 / New aggregate with an incremented version.
        @raise ValueError 会话关闭、过期或玩家重复下注 / Closed, expired, or duplicate wager.
        """

        if self.status is not GameSessionStatus.ACTIVE:
            raise ValueError("Gamble session is not active")
        if now >= self.closes_at:
            raise ValueError("Gamble session has reached its deadline")
        if any(existing.user_id == bet.user_id for existing in self.bets):
            raise ValueError("Player already joined this gamble session")
        return replace(self, bets=(*self.bets, bet), version=self.version + 1)

    def winner_for_ticket(self, ticket: int) -> GambleBet | None:
        """@brief 按下注额权重映射公平整数票 / Map a fair integer ticket by wager weight.

        @param ticket ``[0, prize)`` 内整数 / Integer in ``[0, prize)``.
        @return 中奖下注；空奖池返回 None / Winning bet, or None for an empty pool.
        """

        if not self.bets:
            return None
        if ticket < 0 or ticket >= self.prize:
            raise ValueError("Winner ticket falls outside the prize pool")
        cursor = 0
        for bet in self.bets:
            cursor += bet.amount
            if ticket < cursor:
                return bet
        raise AssertionError("Validated ticket did not map to a wager")


class SicBoBet(StrEnum):
    """@brief 支持的骰宝下注类型 / Supported Sic Bo bet types."""

    BIG = "big"
    SMALL = "small"
    ODD = "odd"
    EVEN = "even"
    SUM_4 = "sum_4"
    SUM_5 = "sum_5"
    SUM_6 = "sum_6"
    SUM_7 = "sum_7"
    SUM_8 = "sum_8"
    SUM_9 = "sum_9"
    SUM_10 = "sum_10"
    SUM_11 = "sum_11"
    SUM_12 = "sum_12"
    SUM_13 = "sum_13"
    SUM_14 = "sum_14"
    SUM_15 = "sum_15"
    SUM_16 = "sum_16"
    SUM_17 = "sum_17"
    ANY_TRIPLE = "any_triple"
    TRIPLE_1 = "triple_1"
    TRIPLE_2 = "triple_2"
    TRIPLE_3 = "triple_3"
    TRIPLE_4 = "triple_4"
    TRIPLE_5 = "triple_5"
    TRIPLE_6 = "triple_6"


SICBO_PAYOUTS: Final[Mapping[SicBoBet, int]] = MappingProxyType(
    {
        SicBoBet.BIG: 1,
        SicBoBet.SMALL: 1,
        SicBoBet.ODD: 1,
        SicBoBet.EVEN: 1,
        SicBoBet.SUM_4: 60,
        SicBoBet.SUM_5: 30,
        SicBoBet.SUM_6: 18,
        SicBoBet.SUM_7: 12,
        SicBoBet.SUM_8: 8,
        SicBoBet.SUM_9: 6,
        SicBoBet.SUM_10: 6,
        SicBoBet.SUM_11: 6,
        SicBoBet.SUM_12: 6,
        SicBoBet.SUM_13: 8,
        SicBoBet.SUM_14: 12,
        SicBoBet.SUM_15: 18,
        SicBoBet.SUM_16: 30,
        SicBoBet.SUM_17: 60,
        SicBoBet.ANY_TRIPLE: 30,
        SicBoBet.TRIPLE_1: 180,
        SicBoBet.TRIPLE_2: 180,
        SicBoBet.TRIPLE_3: 180,
        SicBoBet.TRIPLE_4: 180,
        SicBoBet.TRIPLE_5: 180,
        SicBoBet.TRIPLE_6: 180,
    }
)
"""@brief 旧骰宝净赔率表 / Legacy Sic Bo net-payout table."""


SICBO_BET_NAMES: Final[Mapping[SicBoBet, str]] = MappingProxyType(
    {
        SicBoBet.BIG: "大 (11-17)",
        SicBoBet.SMALL: "小 (4-10)",
        SicBoBet.ODD: "单 (奇数)",
        SicBoBet.EVEN: "双 (偶数)",
        **{SicBoBet(f"sum_{total}"): f"总和{total}" for total in range(4, 18)},
        SicBoBet.ANY_TRIPLE: "任意围骰",
        **{SicBoBet(f"triple_{face}"): f"围骰{face}" for face in range(1, 7)},
    }
)
"""@brief 下注类型中文名 / Chinese names for bet types."""


@dataclass(frozen=True, slots=True)
class DiceRoll:
    """@brief 三枚六面骰的不可变结果 / Immutable roll of three six-sided dice.

    @param dice 三枚点数 / Three die faces.
    """

    dice: tuple[int, int, int]

    def __post_init__(self) -> None:
        """@brief 校验三枚骰子 / Validate the three dice.

        @return None / None.
        """

        if any(face < 1 or face > 6 for face in self.dice):
            raise ValueError("Dice faces must be between one and six")

    @property
    def total(self) -> int:
        """@brief 返回点数和 / Return the face total.

        @return 三枚点数总和 / Sum of the three faces.
        """

        return sum(self.dice)

    @property
    def is_triple(self) -> bool:
        """@brief 判断是否围骰 / Return whether the roll is a triple.

        @return 三枚相同时为 True / True when all faces match.
        """

        return self.dice[0] == self.dice[1] == self.dice[2]

    def wins(self, bet: SicBoBet) -> bool:
        """@brief 按旧规则判断下注是否命中 / Evaluate a wager using legacy rules.

        @param bet 下注类型 / Bet type.
        @return 命中为 True / True when the wager wins.
        @note 围骰使大小皆输，但不影响单双 / Triples lose big/small but retain odd/even behavior.
        """

        if bet is SicBoBet.BIG:
            return 11 <= self.total <= 17 and not self.is_triple
        if bet is SicBoBet.SMALL:
            return 4 <= self.total <= 10 and not self.is_triple
        if bet is SicBoBet.ODD:
            return self.total % 2 == 1
        if bet is SicBoBet.EVEN:
            return self.total % 2 == 0
        if bet is SicBoBet.ANY_TRIPLE:
            return self.is_triple
        if bet.value.startswith("sum_"):
            return self.total == int(bet.value.removeprefix("sum_"))
        if bet.value.startswith("triple_"):
            face = int(bet.value.removeprefix("triple_"))
            return self.is_triple and self.dice[0] == face
        raise AssertionError(f"Unhandled Sic Bo bet: {bet}")

    def features(self) -> tuple[str, ...]:
        """@brief 返回旧 UI 展示的结果特性 / Return legacy UI result features.

        @return 大小、单双与围骰标签 / Big/small, parity, and triple labels.
        """

        labels: list[str] = []
        if 11 <= self.total <= 17 and not self.is_triple:
            labels.append("大")
        elif 4 <= self.total <= 10 and not self.is_triple:
            labels.append("小")
        labels.append("单" if self.total % 2 else "双")
        if self.is_triple:
            labels.append(f"围骰{self.dice[0]}")
        return tuple(labels)


@dataclass(frozen=True, slots=True)
class SicBoSession:
    """@brief 可恢复的骰宝选择聚合 / Recoverable Sic Bo selection aggregate.

    @param session_id 会话 ID / Session ID.
    @param owner_id 玩家 ID / Player ID.
    @param chat_id 展示 chat ID / Display chat ID.
    @param message_id 展示消息 ID / Display message ID.
    @param bet 已选下注类型 / Selected bet type.
    @param expires_at 会话过期时间 / Session expiration.
    @param status 会话状态 / Session status.
    @param version OCC 版本 / OCC version.
    """

    session_id: GameSessionId
    owner_id: int
    chat_id: int
    message_id: int
    expires_at: datetime
    bet: SicBoBet | None = None
    status: GameSessionStatus = GameSessionStatus.ACTIVE
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 校验骰宝聚合 / Validate the Sic Bo aggregate.

        @return None / None.
        """

        if self.owner_id <= 0 or self.message_id <= 0 or self.version < 0:
            raise ValueError("Invalid Sic Bo owner or version")

    def choose(self, bet: SicBoBet, *, now: datetime) -> SicBoSession:
        """@brief 选择下注种类 / Select a bet type.

        @param bet 下注类型 / Bet type.
        @param now 当前时间 / Current time.
        @return 版本递增的新聚合 / New aggregate with incremented version.
        """

        self._require_active(now)
        return replace(self, bet=bet, version=self.version + 1)

    def cancel(self, *, now: datetime) -> SicBoSession:
        """@brief 取消会话 / Cancel the session.

        @param now 当前时间 / Current time.
        @return 已取消聚合 / Cancelled aggregate.
        """

        self._require_active(now)
        return replace(
            self,
            status=GameSessionStatus.CANCELLED,
            version=self.version + 1,
        )

    def _require_active(self, now: datetime) -> None:
        """@brief 校验会话可继续 / Require a live active session.

        @param now 当前时间 / Current time.
        @return None / None.
        """

        if self.status is not GameSessionStatus.ACTIVE:
            raise ValueError("Sic Bo session is not active")
        if now >= self.expires_at:
            raise ValueError("Sic Bo session expired")


@dataclass(frozen=True, slots=True)
class SicBoOutcome:
    """@brief 一次骰宝原子结算结果 / One atomic Sic Bo settlement.

    @param bet 下注类型 / Bet type.
    @param amount 押注金币 / Wager amount.
    @param roll 骰子结果 / Dice roll.
    @param won 是否获胜 / Whether the wager won.
    @param credited 胜利时返还本金加净奖金 / Gross credit on a win.
    """

    bet: SicBoBet
    amount: int
    roll: DiceRoll
    won: bool
    credited: int

    @classmethod
    def resolve(cls, bet: SicBoBet, amount: int, roll: DiceRoll) -> SicBoOutcome:
        """@brief 解析骰宝赔率 / Resolve a Sic Bo wager.

        @param bet 下注类型 / Bet type.
        @param amount 押注额 / Wager amount.
        @param roll 骰子结果 / Dice roll.
        @return 完整结算 / Complete settlement.
        """

        if amount <= 0:
            raise ValueError("Sic Bo wager must be positive")
        won = roll.wins(bet)
        return cls(
            bet=bet,
            amount=amount,
            roll=roll,
            won=won,
            credited=amount * (1 + SICBO_PAYOUTS[bet]) if won else 0,
        )


class FortuneLevel(StrEnum):
    """@brief 御神签运势级别 / Omikuji fortune levels."""

    GREAT_BLESSING = "大吉"
    MIDDLE_BLESSING = "中吉"
    SMALL_BLESSING = "小吉"
    LATE_BLESSING = "末吉"
    CURSE = "凶"
    GREAT_CURSE = "大凶"

    @property
    def is_favorable(self) -> bool:
        """@brief 判断是否为好运签 / Return whether this is a favorable fortune.

        @return 大吉、中吉、小吉为 True / True for the three favorable levels.
        """

        return self in {
            FortuneLevel.GREAT_BLESSING,
            FortuneLevel.MIDDLE_BLESSING,
            FortuneLevel.SMALL_BLESSING,
        }


FORTUNE_WEIGHTS: Final[Mapping[FortuneLevel, int]] = MappingProxyType(
    {
        FortuneLevel.GREAT_BLESSING: 10,
        FortuneLevel.MIDDLE_BLESSING: 20,
        FortuneLevel.SMALL_BLESSING: 30,
        FortuneLevel.LATE_BLESSING: 20,
        FortuneLevel.CURSE: 15,
        FortuneLevel.GREAT_CURSE: 5,
    }
)
"""@brief 旧御神签概率权重 / Legacy Omikuji probability weights."""


def daily_fortune(user_id: int, day: date) -> FortuneLevel:
    """@brief 无全局随机状态地复现每日御神签 / Reproduce daily Omikuji without global RNG state.

    @param user_id 玩家 ID / Player ID.
    @param day 业务日期 / Business date.
    @return 确定性运势 / Deterministic fortune.
    @note 保留旧 MD5 种子与 ``random.choices`` 语义以维持既有每日结果 /
    The legacy MD5 seed and ``random.choices`` semantics are retained for daily-result compatibility.
    """

    if user_id <= 0:
        raise ValueError("Omikuji user_id must be positive")
    seed = f"{user_id}_{day:%Y-%m-%d}"
    digest = int(hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest(), 16)
    generator = random.Random(digest)
    levels = tuple(FORTUNE_WEIGHTS)
    selected = generator.choices(
        levels,
        weights=tuple(FORTUNE_WEIGHTS[level] for level in levels),
        k=1,
    )[0]
    return selected


def daily_fortune_variant(
    user_id: int, day: date, *, choices: int = 4
) -> tuple[int, ...]:
    """@brief 生成稳定文案下标 / Generate stable copy-variant indices.

    @param user_id 玩家 ID / Player ID.
    @param day 业务日期 / Business date.
    @param choices 每一栏的文案数量 / Number of variants per section.
    @return 五个展示栏的稳定下标 / Stable indices for five display sections.
    """

    if choices <= 0:
        raise ValueError("Fortune variant count must be positive")
    seed = f"{user_id}_{day:%Y-%m-%d}"
    digest = int(hashlib.md5(seed.encode(), usedforsecurity=False).hexdigest(), 16)
    generator = random.Random(digest)
    return tuple(generator.randrange(choices) for _ in range(5))


__all__ = [
    "DiceRoll",
    "FORTUNE_WEIGHTS",
    "FortuneLevel",
    "GambleBet",
    "GambleSession",
    "GameSessionId",
    "GameSessionKind",
    "GameSessionStatus",
    "SICBO_BET_NAMES",
    "SICBO_PAYOUTS",
    "SicBoBet",
    "SicBoOutcome",
    "SicBoSession",
    "daily_fortune",
    "daily_fortune_variant",
]
