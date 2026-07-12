"""@brief Crypto 类型化用例与可恢复结算循环 / Typed Crypto use cases and recoverable settlement loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import logging
from typing import Protocol

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.domain.crypto import (
    BTC_PREDICTION_DURATION,
    BTC_PREDICTION_MINIMUM,
    SWAP_MINIMUM,
    ChartToken,
    CoinStake,
    PredictionDirection,
    PriceQuote,
    PredictionOutcome,
    SolanaWalletAddress,
)


logger = logging.getLogger(__name__)

CRYPTO_SERVICE_DATA_KEY = "fogmoe.crypto.service"
"""@brief ``bot_data`` 中 Crypto 服务的稳定键 / Stable Crypto-service key in ``bot_data``."""


class MarketDataUnavailable(RuntimeError):
    """@brief 当前交易所报价暂不可用 / Current exchange quote is temporarily unavailable."""


class CryptoResultCode(StrEnum):
    """@brief Crypto 写用例的穷尽结果代码 / Exhaustive result codes for Crypto write use cases."""

    SUCCESS = "success"
    """@brief 操作成功 / Operation succeeded."""

    NOT_REGISTERED = "not_registered"
    """@brief 账户未注册 / Account is not registered."""

    INSUFFICIENT_COINS = "insufficient_coins"
    """@brief 金币不足 / Insufficient coins."""

    ACTIVE_PREDICTION = "active_prediction"
    """@brief 已有活跃预测 / An active prediction already exists."""

    PENDING_SWAP = "pending_swap"
    """@brief 已有待处理兑换 / A pending swap already exists."""


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """@brief Crypto 所需最小账户快照 / Minimal account snapshot needed by Crypto.

    @param registered 是否已注册 / Whether registered.
    @param balance 免费与付费金币总额 / Total free and paid coin balance.
    """

    registered: bool
    balance: int = 0

    def __post_init__(self) -> None:
        """@brief 校验账户快照 / Validate the account snapshot.

        @return None / None.
        @raise ValueError 余额非法时抛出 / Raised for an invalid balance.
        """

        if self.balance < 0 or (not self.registered and self.balance != 0):
            raise ValueError("Account snapshot is inconsistent")


@dataclass(frozen=True, slots=True)
class ActivePrediction:
    """@brief 用户尚未到期的 BTC 预测 / User's unexpired BTC prediction.

    @param direction 预测方向 / Predicted direction.
    @param amount 投入金币 / Coin stake.
    @param start_price 起始报价 / Starting quote.
    @param started_at 开始时间 / Start time.
    @param due_at 到期时间 / Due time.
    """

    direction: PredictionDirection
    amount: CoinStake
    start_price: PriceQuote
    started_at: datetime
    due_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范时间并校验窗口 / Normalize timestamps and validate the window.

        @return None / None.
        @raise ValueError 到期时间不晚于开始时间时抛出 / Raised when due time is not after start time.
        """

        started_at = _utc(self.started_at)
        due_at = _utc(self.due_at)
        if due_at <= started_at:
            raise ValueError("Prediction due time must follow its start time")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "due_at", due_at)


@dataclass(frozen=True, slots=True)
class PredictionOverview:
    """@brief `/btc_predict` 入口所需快照 / Snapshot required by the `/btc_predict` entry point.

    @param account 账户快照 / Account snapshot.
    @param active 可选活跃预测 / Optional active prediction.
    @param quote 无活跃预测时的当前报价 / Current quote when no prediction is active.
    """

    account: AccountSnapshot
    active: ActivePrediction | None = None
    quote: PriceQuote | None = None


@dataclass(frozen=True, slots=True)
class BindChartToken:
    """@brief 绑定群组图表命令 / Bind-group-chart command.

    @param group_id 群组 ID / Group ID.
    @param actor_id 操作者 ID / Actor ID.
    @param token 规范代币 / Canonical token.
    @param idempotency_key Telegram Update 幂等键 / Telegram Update idempotency key.
    """

    group_id: int
    actor_id: int
    token: ChartToken
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验绑定命令 / Validate the binding command.

        @return None / None.
        """

        if self.group_id == 0 or self.actor_id <= 0:
            raise ValueError("Chart binding identifiers are invalid")
        object.__setattr__(self, "idempotency_key", _key(self.idempotency_key))


@dataclass(frozen=True, slots=True)
class ClearChartToken:
    """@brief 清除群组图表命令 / Clear-group-chart command.

    @param group_id 群组 ID / Group ID.
    @param actor_id 操作者 ID / Actor ID.
    @param idempotency_key Telegram Update 幂等键 / Telegram Update idempotency key.
    """

    group_id: int
    actor_id: int
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验清除命令 / Validate the clear command.

        @return None / None.
        """

        if self.group_id == 0 or self.actor_id <= 0:
            raise ValueError("Chart clear identifiers are invalid")
        object.__setattr__(self, "idempotency_key", _key(self.idempotency_key))


@dataclass(frozen=True, slots=True)
class ChartMutationResult:
    """@brief 图表绑定写入结果 / Chart-binding mutation result.

    @param token 写入后的绑定；清除后为 None / Binding after mutation; None after clear.
    @param replayed 是否来自幂等回放 / Whether returned from an idempotent replay.
    """

    token: ChartToken | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TokenSwapRequest:
    """@brief 待处理 FOGMOE 兑换请求 / Pending FOGMOE token-swap request.

    @param request_id 数据库请求 ID / Database request ID.
    @param amount 兑换金币数 / Coin amount.
    @param wallet 收款钱包 / Recipient wallet.
    @param requested_at 申请时间 / Request time.
    """

    request_id: int
    amount: CoinStake
    wallet: SolanaWalletAddress
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验请求快照 / Validate the request snapshot.

        @return None / None.
        """

        if self.request_id <= 0:
            raise ValueError("Swap request ID must be positive")
        object.__setattr__(self, "requested_at", _utc(self.requested_at))


@dataclass(frozen=True, slots=True)
class SubmitTokenSwap:
    """@brief 提交 FOGMOE 兑换命令 / Submit FOGMOE token-swap command.

    @param user_id 用户 ID / User ID.
    @param username Telegram 用户名 / Telegram username.
    @param wallet 收款钱包 / Recipient wallet.
    @param amount 兑换金币数 / Coin amount.
    @param idempotency_key Telegram Update 幂等键 / Telegram Update idempotency key.
    """

    user_id: int
    username: str
    wallet: SolanaWalletAddress
    amount: CoinStake
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验兑换命令 / Validate the swap command.

        @return None / None.
        @raise ValueError 数量低于产品下限时抛出 / Raised when the amount is below the product minimum.
        """

        if self.user_id <= 0:
            raise ValueError("Swap user ID must be positive")
        username = self.username.strip() or "Unknown"
        if len(username) > 255:
            raise ValueError("Swap username cannot exceed 255 characters")
        if int(self.amount) < SWAP_MINIMUM:
            raise ValueError(f"Swap amount must be at least {SWAP_MINIMUM}")
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "idempotency_key", _key(self.idempotency_key))


@dataclass(frozen=True, slots=True)
class SwapSubmissionResult:
    """@brief 兑换提交的穷尽结果 / Exhaustive token-swap submission result.

    @param code 结果代码 / Result code.
    @param request 当前或新建请求 / Current or newly created request.
    @param balance 判定时余额 / Balance at decision time.
    @param replayed 是否为幂等回放 / Whether this is an idempotent replay.
    """

    code: CryptoResultCode
    request: TokenSwapRequest | None = None
    balance: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class CreateBtcPrediction:
    """@brief 创建 BTC 预测命令 / Create-BTC-prediction command.

    @param user_id 用户 ID / User ID.
    @param chat_id 结果投递 chat ID / Result-delivery chat ID.
    @param direction 预测方向 / Predicted direction.
    @param amount 投入金币 / Coin stake.
    @param requested_at 入口接收时间 / Ingress receipt time.
    @param idempotency_key Telegram Update 幂等键 / Telegram Update idempotency key.
    """

    user_id: int
    chat_id: int
    direction: PredictionDirection
    amount: CoinStake
    requested_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验创建命令 / Validate the creation command.

        @return None / None.
        @raise ValueError 数量低于产品下限时抛出 / Raised when the stake is below the product minimum.
        """

        if self.user_id <= 0 or self.chat_id == 0:
            raise ValueError("Prediction user and chat identifiers are invalid")
        if int(self.amount) < BTC_PREDICTION_MINIMUM:
            raise ValueError(
                f"Prediction stake must be at least {BTC_PREDICTION_MINIMUM}"
            )
        object.__setattr__(self, "requested_at", _utc(self.requested_at))
        object.__setattr__(self, "idempotency_key", _key(self.idempotency_key))

    @property
    def due_at(self) -> datetime:
        """@brief 返回固定十分钟到期点 / Return the fixed ten-minute due time.

        @return 到期时间 / Due time.
        """

        return self.requested_at + BTC_PREDICTION_DURATION


@dataclass(frozen=True, slots=True)
class PredictionCreationResult:
    """@brief 创建 BTC 预测的穷尽结果 / Exhaustive BTC-prediction creation result.

    @param code 结果代码 / Result code.
    @param prediction 新建或已存在预测 / Newly created or existing prediction.
    @param balance 判定时账户余额 / Account balance at decision time.
    @param replayed 是否为幂等回放 / Whether this is an idempotent replay.
    """

    code: CryptoResultCode
    prediction: ActivePrediction | None = None
    balance: int = 0
    replayed: bool = False


class _Accounts(Protocol):
    """@brief workflow 内部账户结构端口 / Structural account port used by the workflow."""

    async def account_snapshot(self, user_id: int) -> AccountSnapshot: ...


class _Charts(Protocol):
    """@brief workflow 内部图表结构端口 / Structural chart port used by the workflow."""

    async def chart_token(self, group_id: int) -> ChartToken | None: ...
    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult: ...
    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult: ...


class _Predictions(Protocol):
    """@brief workflow 内部预测结构端口 / Structural prediction port used by the workflow."""

    async def active_prediction(
        self, user_id: int, *, now: datetime
    ) -> ActivePrediction | None: ...
    async def create_prediction(
        self, command: CreateBtcPrediction, *, quote: PriceQuote
    ) -> PredictionCreationResult: ...
    async def has_due_prediction(self, *, now: datetime) -> bool: ...
    async def settle_due_predictions(
        self, *, quote: PriceQuote, settled_at: datetime, limit: int
    ) -> int: ...


class _Swaps(Protocol):
    """@brief workflow 内部兑换结构端口 / Structural swap port used by the workflow."""

    async def pending_swap(self, user_id: int) -> TokenSwapRequest | None: ...
    async def submit_swap(self, command: SubmitTokenSwap) -> SwapSubmissionResult: ...


class _Prices(Protocol):
    """@brief workflow 内部使用的报价结构端口 / Structural quote port used by the workflow."""

    async def current_price(self) -> PriceQuote: ...


class CryptoService:
    """@brief 无 Telegram/SQL 依赖的 Crypto 应用服务 / Crypto application service without Telegram or SQL dependencies."""

    def __init__(
        self,
        *,
        accounts: _Accounts,
        charts: _Charts,
        predictions: _Predictions,
        swaps: _Swaps,
        prices: _Prices,
        poll_interval: float = 2.0,
        failure_interval: float = 10.0,
        settlement_batch_size: int = 50,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 创建服务及其受监督结算循环 / Create the service and its supervised settlement loop.

        @param accounts 账户读取端口 / Account-read port.
        @param charts 图表原子操作 / Atomic chart operations.
        @param predictions 预测创建与结算操作 / Prediction creation and settlement operations.
        @param swaps 兑换原子操作 / Atomic swap operations.
        @param prices BTC 报价端口 / BTC quote port.
        @param poll_interval 无到期记录时轮询秒数 / Poll interval when no record is due.
        @param failure_interval 瞬态依赖失败后的有界等待秒数 / Bounded delay after a transient dependency failure.
        @param settlement_batch_size 单事务最大结算数 / Maximum settlements per transaction.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        """

        if poll_interval <= 0 or failure_interval <= 0:
            raise ValueError("Crypto polling intervals must be positive")
        if settlement_batch_size < 1:
            raise ValueError("Crypto settlement batch size must be positive")
        self._accounts = accounts
        self._charts = charts
        self._predictions = predictions
        self._swaps = swaps
        self._prices = prices
        self._poll_interval = poll_interval
        self._failure_interval = failure_interval
        self._settlement_batch_size = settlement_batch_size
        self._clock = clock or SystemUtcClock()

    async def prediction_overview(self, user_id: int) -> PredictionOverview:
        """@brief 读取预测菜单状态，仅在需要时请求交易所 / Read prediction-menu state and quote only when needed.

        @param user_id 用户 ID / User ID.
        @return 账户、活跃预测或当前报价 / Account, active prediction, or current quote.
        """

        account = await self._accounts.account_snapshot(user_id)
        if not account.registered:
            return PredictionOverview(account)
        active = await self._predictions.active_prediction(
            user_id,
            now=self._clock.now(),
        )
        if active is not None:
            return PredictionOverview(account, active=active)
        return PredictionOverview(account, quote=await self._prices.current_price())

    async def quote_for_stake(
        self,
        user_id: int,
        amount: CoinStake,
    ) -> PredictionOverview:
        """@brief 校验账户余额并返回方向选择所需报价 / Validate balance and return the quote needed for direction selection.

        @param user_id 用户 ID / User ID.
        @param amount 候选投入 / Candidate stake.
        @return 账户、可选活跃预测与可选报价 / Account, optional active prediction, and optional quote.
        """

        if int(amount) < BTC_PREDICTION_MINIMUM:
            raise ValueError(
                f"Prediction stake must be at least {BTC_PREDICTION_MINIMUM}"
            )
        return await self.prediction_overview(user_id)

    async def create_prediction(
        self,
        command: CreateBtcPrediction,
    ) -> PredictionCreationResult:
        """@brief 获取报价后执行原子创建 / Fetch a quote and execute atomic creation.

        @param command 创建命令 / Creation command.
        @return 创建结果 / Creation result.
        """

        quote = await self._prices.current_price()
        return await self._predictions.create_prediction(command, quote=quote)

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 读取群组图表绑定 / Read a group's chart binding.

        @param group_id 群组 ID / Group ID.
        @return 绑定或 None / Binding or None.
        """

        return await self._charts.chart_token(group_id)

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 写入群组图表绑定 / Write a group's chart binding.

        @param command 绑定命令 / Binding command.
        @return 写入结果 / Mutation result.
        """

        return await self._charts.bind_chart(command)

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 清除群组图表绑定 / Clear a group's chart binding.

        @param command 清除命令 / Clear command.
        @return 清除结果 / Mutation result.
        """

        return await self._charts.clear_chart(command)

    async def account_snapshot(self, user_id: int) -> AccountSnapshot:
        """@brief 读取兑换入口账户 / Read the account required by the swap entry point.

        @param user_id 用户 ID / User ID.
        @return 账户快照 / Account snapshot.
        """

        return await self._accounts.account_snapshot(user_id)

    async def pending_swap(self, user_id: int) -> TokenSwapRequest | None:
        """@brief 读取待处理兑换 / Read a pending swap.

        @param user_id 用户 ID / User ID.
        @return 待处理请求或 None / Pending request or None.
        """

        return await self._swaps.pending_swap(user_id)

    async def submit_swap(self, command: SubmitTokenSwap) -> SwapSubmissionResult:
        """@brief 原子提交兑换 / Atomically submit a swap.

        @param command 兑换命令 / Swap command.
        @return 提交结果 / Submission result.
        """

        return await self._swaps.submit_swap(command)

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行受 BotRuntime 监督的 durable 预测结算循环 / Run the BotRuntime-supervised durable prediction settlement loop.

        @param stop_event 阶段停止信号 / Phase stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            try:
                now = self._clock.now()
                if not await self._predictions.has_due_prediction(now=now):
                    await _wait_or_stop(stop_event, self._poll_interval)
                    continue
                quote = await self._prices.current_price()
            except MarketDataUnavailable as error:
                logger.warning("BTC settlement quote unavailable: %s", error)
                await _wait_or_stop(stop_event, self._failure_interval)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTC prediction settlement pass failed")
                await _wait_or_stop(stop_event, self._failure_interval)
                continue
            try:
                settled = await self._predictions.settle_due_predictions(
                    quote=quote,
                    settled_at=self._clock.now(),
                    limit=self._settlement_batch_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTC prediction settlement commit failed")
                await _wait_or_stop(stop_event, self._failure_interval)
                continue
            if settled < self._settlement_batch_size:
                await _wait_or_stop(stop_event, self._poll_interval)


def render_prediction_outcome(
    outcome: PredictionOutcome,
    *,
    display_name: str,
) -> str:
    """@brief 将结算领域结果投影为渠道无关纯文本 / Project a settlement result into channel-neutral plain text.

    @param outcome 领域结算结果 / Domain settlement result.
    @param display_name 用户展示名称 / User display name.
    @return 用户可见通知 / User-visible notification.
    """

    direction = "上涨 ↗" if outcome.direction is PredictionDirection.UP else "下跌 ↘"
    actual_direction = (
        "上涨 ↗" if outcome.end_price.value > outcome.start_price.value else "下跌 ↘"
    )
    change_percent = abs(outcome.change_percent)
    identity = display_name.strip() or f"用户 {outcome.user_id}"
    if outcome.correct:
        result = (
            f"🎉 {identity}，您的比特币价格预测正确！\n\n"
            f"预测方向: {direction}\n"
            f"实际变化: {actual_direction} ({change_percent:.2f}%)\n"
            f"起始价格: ${outcome.start_price.value:,.2f}\n"
            f"结束价格: ${outcome.end_price.value:,.2f}\n\n"
            f"您获得了 {outcome.reward} 金币 (本金 + 80% 奖励)！"
        )
    else:
        result = (
            f"😞 {identity}，您的比特币价格预测错误。\n\n"
            f"预测方向: {direction}\n"
            f"实际变化: {actual_direction} ({change_percent:.2f}%)\n"
            f"起始价格: ${outcome.start_price.value:,.2f}\n"
            f"结束价格: ${outcome.end_price.value:,.2f}\n\n"
            f"您损失了投入的 {int(outcome.amount)} 金币。再接再厉！"
        )
    return (
        result + "\n\n比特币实时价格图表: "
        "https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P"
    )


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """@brief 等待停止事件或短轮询定时器 / Wait for a stop event or short polling timer.

    @param stop_event 停止信号 / Stop signal.
    @param delay 最大等待秒数 / Maximum delay in seconds.
    @return None / None.
    """

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return


def _key(value: str) -> str:
    """@brief 规范持久化幂等键 / Normalize a persisted idempotency key.

    @param value 输入键 / Input key.
    @return 规范键 / Canonical key.
    @raise ValueError 键为空或过长时抛出 / Raised for an empty or oversized key.
    """

    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("Idempotency key must contain 1-512 characters")
    return normalized


def _utc(value: datetime) -> datetime:
    """@brief 规范为 UTC aware datetime / Normalize to a UTC-aware datetime.

    @param value 输入时间 / Input datetime.
    @return UTC 时间 / UTC datetime.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "CRYPTO_SERVICE_DATA_KEY",
    "AccountSnapshot",
    "ActivePrediction",
    "BindChartToken",
    "ChartMutationResult",
    "ClearChartToken",
    "CreateBtcPrediction",
    "CryptoResultCode",
    "CryptoService",
    "MarketDataUnavailable",
    "PredictionCreationResult",
    "PredictionOverview",
    "SubmitTokenSwap",
    "SwapSubmissionResult",
    "TokenSwapRequest",
    "render_prediction_outcome",
]
