"""@brief 石头剪刀布纯领域聚合 / Pure rock-paper-scissors domain aggregate.

本模块只描述游戏身份、状态和转移，不知道 Telegram、数据库、事件循环或应用服务。
/ This module describes game identity, state, and transitions only. It has no knowledge of
Telegram, databases, event loops, or application services.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
import re


ENTRY_FEE = 1
"""@brief 每位玩家的入场费 / Entry fee charged to each player."""

WINNER_REWARD = 2
"""@brief 非平局时赢家获得的金币 / Coins awarded to the winner of a non-draw game."""

_GAME_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,24}$")
"""@brief callback-safe 游戏标识格式 / Callback-safe game-identifier format."""


class RpsDomainError(ValueError):
    """@brief 猜拳领域不变量或转移错误 / RPS invariant or transition error."""


class StaleGameVersion(RpsDomainError):
    """@brief 操作引用的聚合版本已经过期 / Operation references a stale aggregate version."""


@dataclass(frozen=True, slots=True, order=True)
class UserId:
    """@brief 玩家身份值对象 / Player-identity value object.

    @param value Telegram 用户的正整数稳定标识 / Positive stable Telegram user identifier.
    """

    value: int
    """@brief 用户标识原始值 / Raw user-identifier value."""

    def __post_init__(self) -> None:
        """@brief 校验用户标识 / Validate the user identifier.

        @return None / None.
        @raises TypeError 标识不是严格整数 / If the identifier is not a strict integer.
        @raises ValueError 标识不是正数 / If the identifier is not positive.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("user id must be an integer")
        if self.value <= 0:
            raise ValueError("user id must be positive")

    def __int__(self) -> int:
        """@brief 返回基础整数 / Return the underlying integer.

        @return 用户标识整数 / User-identifier integer.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """@brief 玩家账户的游戏准入快照 / Game-admission snapshot of a player account.

    @param registered 账户是否存在 / Whether the account exists.
    @param coins 当前可用总金币 / Current total spendable coins.
    """

    registered: bool
    """@brief 注册状态 / Registration status."""

    coins: int
    """@brief 可用总金币 / Total spendable coins."""

    def __post_init__(self) -> None:
        """@brief 校验账户快照 / Validate the account snapshot.

        @return None / None.
        @raises TypeError 字段类型无效 / If a field has an invalid type.
        @raises ValueError 金币为负 / If coins are negative.
        """

        if not isinstance(self.registered, bool):
            raise TypeError("registered must be a bool")
        if isinstance(self.coins, bool) or not isinstance(self.coins, int):
            raise TypeError("coins must be an integer")
        if self.coins < 0:
            raise ValueError("coins must not be negative")


@dataclass(frozen=True, slots=True, order=True)
class GameId:
    """@brief callback-safe 游戏身份值对象 / Callback-safe game-identity value object.

    @param value 8 到 24 字节的 URL-safe 标识 / URL-safe identifier of 8 to 24 ASCII bytes.
    """

    value: str
    """@brief 游戏标识原始值 / Raw game-identifier value."""

    def __post_init__(self) -> None:
        """@brief 校验游戏标识 / Validate the game identifier.

        @return None / None.
        @raises TypeError 标识不是字符串 / If the identifier is not a string.
        @raises ValueError 标识不满足 callback-safe 格式 / If the identifier is not callback-safe.
        """

        if not isinstance(self.value, str):
            raise TypeError("game id must be a string")
        if _GAME_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("game id must contain 8-24 URL-safe ASCII characters")

    def __str__(self) -> str:
        """@brief 返回可编码标识 / Return the encodable identifier.

        @return 游戏标识字符串 / Game-identifier string.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class GameVersion:
    """@brief 聚合乐观并发版本 / Aggregate optimistic-concurrency version.

    @param value 从零开始的单调版本 / Monotonic version starting at zero.
    """

    value: int
    """@brief 版本原始值 / Raw version value."""

    def __post_init__(self) -> None:
        """@brief 校验版本 / Validate the version.

        @return None / None.
        @raises TypeError 版本不是严格整数 / If the version is not a strict integer.
        @raises ValueError 版本为负数 / If the version is negative.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("game version must be an integer")
        if self.value < 0:
            raise ValueError("game version must not be negative")

    def next(self) -> GameVersion:
        """@brief 创建下一版本 / Create the next version.

        @return 严格递增一的版本 / Version incremented by exactly one.
        """

        return GameVersion(self.value + 1)


@dataclass(frozen=True, slots=True)
class Player:
    """@brief 对局玩家 / Game player.

    @param user_id 强类型玩家身份 / Strongly typed player identity.
    @param display_name 用于界面展示的非空名称 / Non-empty name used for presentation.
    """

    user_id: UserId
    """@brief 玩家身份 / Player identity."""

    display_name: str
    """@brief 玩家展示名称 / Player display name."""

    def __post_init__(self) -> None:
        """@brief 校验玩家快照 / Validate the player snapshot.

        @return None / None.
        @raises TypeError 字段类型错误 / If a field has the wrong type.
        @raises ValueError 展示名称为空或过长 / If the display name is blank or too long.
        """

        if not isinstance(self.user_id, UserId):
            raise TypeError("user_id must be a UserId")
        if not isinstance(self.display_name, str):
            raise TypeError("display_name must be a string")
        normalized = self.display_name.strip().lstrip("@").strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        if len(normalized) > 128:
            raise ValueError("display_name must not exceed 128 characters")
        object.__setattr__(self, "display_name", normalized)


class Choice(StrEnum):
    """@brief 玩家可选手势 / Hand shapes available to a player."""

    ROCK = "rock"
    """@brief 石头 / Rock."""

    PAPER = "paper"
    """@brief 布 / Paper."""

    SCISSORS = "scissors"
    """@brief 剪刀 / Scissors."""

    def beats(self, other: Choice) -> bool:
        """@brief 判断当前手势是否击败另一手势 / Decide whether this choice beats another.

        @param other 对手手势 / Opponent choice.
        @return 当前手势获胜时为 True / True when this choice wins.
        """

        return (self, other) in {
            (Choice.ROCK, Choice.SCISSORS),
            (Choice.PAPER, Choice.ROCK),
            (Choice.SCISSORS, Choice.PAPER),
        }


class GameStatus(StrEnum):
    """@brief 对局生命周期状态 / Game-session lifecycle state."""

    CHOOSING = "choosing"
    """@brief 等待玩家出招 / Waiting for choices."""

    FINISHED = "finished"
    """@brief 已结算 / Settled."""

    CANCELLED = "cancelled"
    """@brief 已取消且应退款 / Cancelled and refundable."""


class GameCancellation(StrEnum):
    """@brief 对局取消原因 / Reason a game session was cancelled."""

    TIMEOUT = "timeout"
    """@brief 玩家选择超时 / Player-choice timeout."""

    DELIVERY_FAILED = "delivery_failed"
    """@brief 关键交互消息投递失败 / Critical interaction delivery failed."""

    SERVICE_SHUTDOWN = "service_shutdown"
    """@brief 应用服务正在关闭 / Application service is shutting down."""


class OutcomeKind(StrEnum):
    """@brief 已完成对局的结果类别 / Outcome kind of a finished game."""

    DRAW = "draw"
    """@brief 平局 / Draw."""

    PLAYER_ONE = "player_one"
    """@brief 玩家一获胜 / Player one wins."""

    PLAYER_TWO = "player_two"
    """@brief 玩家二获胜 / Player two wins."""


@dataclass(frozen=True, slots=True)
class Payout:
    """@brief 一笔领域结算 / One domain payout.

    @param user_id 收款玩家 / Recipient player.
    @param coins 正整数金币数 / Positive coin amount.
    """

    user_id: UserId
    """@brief 收款玩家身份 / Recipient identity."""

    coins: int
    """@brief 收款金币数 / Coin amount."""

    def __post_init__(self) -> None:
        """@brief 校验结算 / Validate the payout.

        @return None / None.
        @raises TypeError 字段类型错误 / If a field has the wrong type.
        @raises ValueError 金币数不为正 / If the coin amount is not positive.
        """

        if not isinstance(self.user_id, UserId):
            raise TypeError("payout user_id must be a UserId")
        if isinstance(self.coins, bool) or not isinstance(self.coins, int):
            raise TypeError("payout coins must be an integer")
        if self.coins <= 0:
            raise ValueError("payout coins must be positive")


@dataclass(frozen=True, slots=True)
class GameOutcome:
    """@brief 已完成对局的不可变结果 / Immutable result of a finished game.

    @param kind 结果类别 / Outcome kind.
    @param winner 赢家；平局为 None / Winner, or None for a draw.
    @param payouts 守恒为两枚金币的结算 / Payouts conserving the two-coin pot.
    """

    kind: OutcomeKind
    """@brief 结果类别 / Outcome kind."""

    winner: UserId | None
    """@brief 获胜玩家 / Winning player."""

    payouts: tuple[Payout, ...]
    """@brief 结算列表 / Payout list."""

    def __post_init__(self) -> None:
        """@brief 校验结果与结算守恒 / Validate outcome and payout conservation.

        @return None / None.
        @raises RpsDomainError 赢家语义或金币守恒被破坏 / If winner semantics or coin conservation is broken.
        """

        if not isinstance(self.kind, OutcomeKind):
            raise TypeError("outcome kind must be an OutcomeKind")
        if self.winner is not None and not isinstance(self.winner, UserId):
            raise TypeError("winner must be a UserId or None")
        if not self.payouts or any(
            not isinstance(payout, Payout) for payout in self.payouts
        ):
            raise TypeError("payouts must contain Payout values")
        if sum(payout.coins for payout in self.payouts) != ENTRY_FEE * 2:
            raise RpsDomainError("payouts must conserve the two-coin pot")
        if self.kind is OutcomeKind.DRAW:
            if self.winner is not None or len(self.payouts) != 2:
                raise RpsDomainError(
                    "a draw must refund both players and have no winner"
                )
        elif self.winner is None or self.payouts != (
            Payout(self.winner, WINNER_REWARD),
        ):
            raise RpsDomainError(
                "a winning outcome must award the entire pot to its winner"
            )


@dataclass(frozen=True, slots=True)
class WaitingRoom:
    """@brief 等待第二位玩家的开放房间 / Open room waiting for a second player.

    @param game_id 从邀请到结算保持稳定的游戏身份 / Stable identity from invitation through settlement.
    @param version 当前聚合版本 / Current aggregate version.
    @param host 创建邀请的玩家 / Player who created the invitation.
    @param created_at 创建时间 / Creation time.
    @param expires_at 等待房间失效时间 / Waiting-room expiration time.
    """

    game_id: GameId
    """@brief 游戏身份 / Game identity."""

    version: GameVersion
    """@brief 聚合版本 / Aggregate version."""

    host: Player
    """@brief 房主 / Room host."""

    created_at: datetime
    """@brief 创建时间 / Creation time."""

    expires_at: datetime
    """@brief 失效时间 / Expiration time."""

    def __post_init__(self) -> None:
        """@brief 校验等待房间不变量 / Validate waiting-room invariants.

        @return None / None.
        @raises TypeError 字段类型错误 / If a field has the wrong type.
        @raises RpsDomainError 时间或初始版本无效 / If time ordering or initial version is invalid.
        """

        if not isinstance(self.game_id, GameId):
            raise TypeError("game_id must be a GameId")
        if not isinstance(self.version, GameVersion):
            raise TypeError("version must be a GameVersion")
        if not isinstance(self.host, Player):
            raise TypeError("host must be a Player")
        _validate_aware_datetime(self.created_at, "created_at")
        _validate_aware_datetime(self.expires_at, "expires_at")
        if self.version != GameVersion(0):
            raise RpsDomainError("a waiting room must start at version zero")
        if self.expires_at <= self.created_at:
            raise RpsDomainError("waiting-room expiration must follow creation")

    @classmethod
    def open(
        cls,
        game_id: GameId,
        host: Player,
        *,
        now: datetime,
        wait_for: timedelta,
    ) -> WaitingRoom:
        """@brief 创建开放等待房间 / Open a waiting room.

        @param game_id 新游戏身份 / New game identity.
        @param host 房主 / Room host.
        @param now 创建时刻 / Creation instant.
        @param wait_for 等待第二位玩家的时长 / Duration to wait for a second player.
        @return 已校验的等待房间 / Validated waiting room.
        @raises ValueError 等待时长不为正 / If the waiting duration is not positive.
        """

        if wait_for <= timedelta(0):
            raise ValueError("wait_for must be positive")
        return cls(
            game_id=game_id,
            version=GameVersion(0),
            host=host,
            created_at=now,
            expires_at=now + wait_for,
        )

    def join(
        self,
        guest: Player,
        *,
        expected_version: GameVersion,
        now: datetime,
        choose_for: timedelta,
    ) -> GameSession:
        """@brief 由第二位玩家加入并进入选择阶段 / Join and enter the choice phase.

        @param guest 加入玩家 / Joining player.
        @param expected_version callback 观察到的版本 / Version observed by the callback.
        @param now 加入时刻 / Join instant.
        @param choose_for 双方选择时限 / Time allowed for both choices.
        @return 初始对局会话 / Initial game session.
        @raises StaleGameVersion callback 已过期 / If the callback version is stale.
        @raises RpsDomainError 自己加入、房间过期或选择时长无效 / If self-joining, expiry, or duration is invalid.
        """

        self._require_version(expected_version)
        _validate_aware_datetime(now, "now")
        if now >= self.expires_at:
            raise RpsDomainError("waiting room has expired")
        if guest.user_id == self.host.user_id:
            raise RpsDomainError("the host cannot join their own room")
        if choose_for <= timedelta(0):
            raise ValueError("choose_for must be positive")
        return GameSession(
            game_id=self.game_id,
            version=self.version.next(),
            player_one=self.host,
            player_two=guest,
            status=GameStatus.CHOOSING,
            player_one_choice=None,
            player_two_choice=None,
            started_at=now,
            expires_at=now + choose_for,
            outcome=None,
            cancellation=None,
        )

    def _require_version(self, expected_version: GameVersion) -> None:
        """@brief 检查乐观并发版本 / Check the optimistic-concurrency version.

        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @return None / None.
        @raises StaleGameVersion 版本不一致 / If the version differs.
        """

        if expected_version != self.version:
            raise StaleGameVersion(
                f"waiting room is version {self.version.value}, not {expected_version.value}"
            )


@dataclass(frozen=True, slots=True)
class GameSession:
    """@brief 两位玩家的有限状态游戏聚合 / Two-player finite-state game aggregate.

    @param game_id 游戏身份 / Game identity.
    @param version 每次成功转移严格加一的版本 / Version incremented by every successful transition.
    @param player_one 第一位玩家 / First player.
    @param player_two 第二位玩家 / Second player.
    @param status 生命周期状态 / Lifecycle state.
    @param player_one_choice 第一位玩家选择 / First player's choice.
    @param player_two_choice 第二位玩家选择 / Second player's choice.
    @param started_at 对局开始时间 / Game start time.
    @param expires_at 选择阶段截止时间 / Choice-phase deadline.
    @param outcome 完成结果 / Finished outcome.
    @param cancellation 取消原因 / Cancellation reason.
    """

    game_id: GameId
    """@brief 游戏身份 / Game identity."""

    version: GameVersion
    """@brief 聚合版本 / Aggregate version."""

    player_one: Player
    """@brief 第一位玩家 / First player."""

    player_two: Player
    """@brief 第二位玩家 / Second player."""

    status: GameStatus
    """@brief 生命周期状态 / Lifecycle state."""

    player_one_choice: Choice | None
    """@brief 第一位玩家选择 / First-player choice."""

    player_two_choice: Choice | None
    """@brief 第二位玩家选择 / Second-player choice."""

    started_at: datetime
    """@brief 开始时间 / Start time."""

    expires_at: datetime
    """@brief 截止时间 / Deadline."""

    outcome: GameOutcome | None
    """@brief 完成结果 / Finished outcome."""

    cancellation: GameCancellation | None
    """@brief 取消原因 / Cancellation reason."""

    def __post_init__(self) -> None:
        """@brief 校验全部状态组合 / Validate every state combination.

        @return None / None.
        @raises RpsDomainError 状态字段组合不可能 / If state fields form an impossible combination.
        """

        if not isinstance(self.game_id, GameId):
            raise TypeError("game_id must be a GameId")
        if not isinstance(self.version, GameVersion):
            raise TypeError("version must be a GameVersion")
        if not isinstance(self.player_one, Player) or not isinstance(
            self.player_two, Player
        ):
            raise TypeError("both players must be Player values")
        if self.player_one.user_id == self.player_two.user_id:
            raise RpsDomainError("a game requires two distinct players")
        if not isinstance(self.status, GameStatus):
            raise TypeError("status must be a GameStatus")
        if self.player_one_choice is not None and not isinstance(
            self.player_one_choice, Choice
        ):
            raise TypeError("player_one_choice must be a Choice or None")
        if self.player_two_choice is not None and not isinstance(
            self.player_two_choice, Choice
        ):
            raise TypeError("player_two_choice must be a Choice or None")
        _validate_aware_datetime(self.started_at, "started_at")
        _validate_aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.started_at:
            raise RpsDomainError("game expiration must follow game start")
        if self.status is GameStatus.CHOOSING:
            if self.outcome is not None or self.cancellation is not None:
                raise RpsDomainError(
                    "an active game cannot have an outcome or cancellation"
                )
            if (
                self.player_one_choice is not None
                and self.player_two_choice is not None
            ):
                raise RpsDomainError(
                    "two choices must transition immediately to FINISHED"
                )
        elif self.status is GameStatus.FINISHED:
            if (
                self.player_one_choice is None
                or self.player_two_choice is None
                or self.outcome is None
                or self.cancellation is not None
            ):
                raise RpsDomainError(
                    "a finished game requires both choices and one outcome"
                )
            player_ids = {self.player_one.user_id, self.player_two.user_id}
            if any(payout.user_id not in player_ids for payout in self.outcome.payouts):
                raise RpsDomainError("an outcome may only pay participating players")
        elif self.status is GameStatus.CANCELLED:
            if self.outcome is not None or self.cancellation is None:
                raise RpsDomainError(
                    "a cancelled game requires one cancellation and no outcome"
                )

    @property
    def players(self) -> tuple[Player, Player]:
        """@brief 返回稳定玩家顺序 / Return players in stable order.

        @return 玩家一与玩家二 / Player one and player two.
        """

        return (self.player_one, self.player_two)

    @property
    def refunds(self) -> tuple[Payout, Payout]:
        """@brief 返回取消时的入场费退款 / Return entry-fee refunds for cancellation.

        @return 双方各一枚金币 / One coin for each player.
        """

        return (
            Payout(self.player_one.user_id, ENTRY_FEE),
            Payout(self.player_two.user_id, ENTRY_FEE),
        )

    def choice_for(self, user_id: UserId) -> Choice | None:
        """@brief 查询指定玩家的选择 / Read a player's choice.

        @param user_id 玩家身份 / Player identity.
        @return 当前选择；尚未选择时为 None / Current choice, or None when pending.
        @raises RpsDomainError 用户不是本局玩家 / If the user does not participate.
        """

        if user_id == self.player_one.user_id:
            return self.player_one_choice
        if user_id == self.player_two.user_id:
            return self.player_two_choice
        raise RpsDomainError("user is not a participant in this game")

    def opponent_of(self, user_id: UserId) -> Player:
        """@brief 查询对手 / Return a player's opponent.

        @param user_id 玩家身份 / Player identity.
        @return 对手玩家 / Opponent player.
        @raises RpsDomainError 用户不是本局玩家 / If the user does not participate.
        """

        if user_id == self.player_one.user_id:
            return self.player_two
        if user_id == self.player_two.user_id:
            return self.player_one
        raise RpsDomainError("user is not a participant in this game")

    def choose(
        self,
        user_id: UserId,
        choice: Choice,
        *,
        expected_version: GameVersion,
        now: datetime,
    ) -> GameSession:
        """@brief 应用一次玩家选择 / Apply one player choice.

        @param user_id 出招玩家 / Acting player.
        @param choice 玩家手势 / Player choice.
        @param expected_version callback 观察到的版本 / Version observed by the callback.
        @param now 转移时刻 / Transition instant.
        @return 选择后的新聚合；双方已选时立即完成 / New aggregate, finished immediately when both chose.
        @raises StaleGameVersion callback 已过期 / If the callback version is stale.
        @raises RpsDomainError 状态、身份、重复选择或超时无效 / If state, actor, duplication, or deadline is invalid.
        """

        self._require_version(expected_version)
        _validate_aware_datetime(now, "now")
        if self.status is not GameStatus.CHOOSING:
            raise RpsDomainError("choices are accepted only while choosing")
        if now >= self.expires_at:
            raise RpsDomainError("game choice deadline has passed")
        if not isinstance(choice, Choice):
            raise TypeError("choice must be a Choice")
        first_choice: Choice | None
        second_choice: Choice | None
        if user_id == self.player_one.user_id:
            if self.player_one_choice is not None:
                raise RpsDomainError("player one has already chosen")
            first_choice = choice
            second_choice = self.player_two_choice
        elif user_id == self.player_two.user_id:
            if self.player_two_choice is not None:
                raise RpsDomainError("player two has already chosen")
            first_choice = self.player_one_choice
            second_choice = choice
        else:
            raise RpsDomainError("user is not a participant in this game")

        next_version = self.version.next()
        if first_choice is None or second_choice is None:
            return replace(
                self,
                version=next_version,
                player_one_choice=first_choice,
                player_two_choice=second_choice,
            )
        outcome = _resolve_choices(
            self.player_one, first_choice, self.player_two, second_choice
        )
        return replace(
            self,
            version=next_version,
            status=GameStatus.FINISHED,
            player_one_choice=first_choice,
            player_two_choice=second_choice,
            outcome=outcome,
        )

    def cancel(
        self,
        reason: GameCancellation,
        *,
        expected_version: GameVersion,
    ) -> GameSession:
        """@brief 取消活动对局 / Cancel an active game.

        @param reason 取消原因 / Cancellation reason.
        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @return 已取消的新聚合 / New cancelled aggregate.
        @raises StaleGameVersion 版本过期 / If the version is stale.
        @raises RpsDomainError 对局不再活动 / If the game is no longer active.
        """

        self._require_version(expected_version)
        if self.status is not GameStatus.CHOOSING:
            raise RpsDomainError("only an active game can be cancelled")
        if not isinstance(reason, GameCancellation):
            raise TypeError("reason must be a GameCancellation")
        return replace(
            self,
            version=self.version.next(),
            status=GameStatus.CANCELLED,
            cancellation=reason,
        )

    def _require_version(self, expected_version: GameVersion) -> None:
        """@brief 检查乐观并发版本 / Check the optimistic-concurrency version.

        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @return None / None.
        @raises StaleGameVersion 版本不一致 / If the version differs.
        """

        if expected_version != self.version:
            raise StaleGameVersion(
                f"game is version {self.version.value}, not {expected_version.value}"
            )


def _validate_aware_datetime(value: datetime, field_name: str) -> None:
    """@brief 验证领域时间带时区 / Require a timezone-aware domain timestamp.

    @param value 待验证时间 / Timestamp to validate.
    @param field_name 错误消息中的字段名 / Field name used in errors.
    @return None / None.
    @raises TypeError 值不是 datetime / If the value is not a datetime.
    @raises ValueError 时间不带时区 / If the timestamp is naive.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _resolve_choices(
    player_one: Player,
    first: Choice,
    player_two: Player,
    second: Choice,
) -> GameOutcome:
    """@brief 解析双方选择后的守恒结算 / Resolve a conservation-safe outcome from both choices.

    @param player_one 第一位玩家 / First player.
    @param first 第一位玩家选择 / First-player choice.
    @param player_two 第二位玩家 / Second player.
    @param second 第二位玩家选择 / Second-player choice.
    @return 不可变结果 / Immutable outcome.
    """

    if first is second:
        return GameOutcome(
            kind=OutcomeKind.DRAW,
            winner=None,
            payouts=(
                Payout(player_one.user_id, ENTRY_FEE),
                Payout(player_two.user_id, ENTRY_FEE),
            ),
        )
    if first.beats(second):
        winner = player_one.user_id
        kind = OutcomeKind.PLAYER_ONE
    else:
        winner = player_two.user_id
        kind = OutcomeKind.PLAYER_TWO
    return GameOutcome(
        kind=kind,
        winner=winner,
        payouts=(Payout(winner, WINNER_REWARD),),
    )
