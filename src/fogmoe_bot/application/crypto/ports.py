"""@brief Crypto 应用层窄端口 / Narrow Crypto application ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.crypto import ChartToken, PriceQuote

from .workflow import (
    AccountSnapshot,
    ActivePrediction,
    BindChartToken,
    ChartMutationResult,
    ClearChartToken,
    CreateBtcPrediction,
    PredictionCreationResult,
    SubmitTokenSwap,
    SwapSubmissionResult,
    TokenSwapRequest,
)


class BtcPriceSource(Protocol):
    """@brief BTC/USDT 当前报价端口 / Current BTC/USDT quote port."""

    async def current_price(self) -> PriceQuote:
        """@brief 获取一个已校验报价 / Fetch one validated quote.

        @return 当前 BTC/USDT 报价 / Current BTC/USDT quote.
        """

        ...


class CryptoAccountReader(Protocol):
    """@brief Crypto 入口所需账户读端口 / Account-read port required by Crypto entry points."""

    async def account_snapshot(self, user_id: int) -> AccountSnapshot:
        """@brief 读取账户存在性与余额 / Read account existence and balance.

        @param user_id 用户 ID / User ID.
        @return 账户快照 / Account snapshot.
        """

        ...


class ChartOperations(Protocol):
    """@brief 群组图表绑定的原子端口 / Atomic group-chart binding port."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 读取群组图表绑定 / Read a group's chart binding.

        @param group_id 群组 ID / Group ID.
        @return 已绑定代币或 None / Bound token or None.
        """

        ...

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 幂等写入图表绑定 / Idempotently write a chart binding.

        @param command 绑定命令 / Binding command.
        @return 写入结果 / Mutation result.
        """

        ...

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 幂等清除图表绑定 / Idempotently clear a chart binding.

        @param command 清除命令 / Clear command.
        @return 清除结果 / Mutation result.
        """

        ...


class PredictionOperations(Protocol):
    """@brief BTC 预测创建与结算端口 / BTC prediction creation and settlement port."""

    async def active_prediction(
        self,
        user_id: int,
        *,
        now: datetime,
    ) -> ActivePrediction | None:
        """@brief 读取尚未到期的预测 / Read an unexpired prediction.

        @param user_id 用户 ID / User ID.
        @param now 当前时间 / Current time.
        @return 活跃预测或 None / Active prediction or None.
        """

        ...

    async def create_prediction(
        self,
        command: CreateBtcPrediction,
        *,
        quote: PriceQuote,
    ) -> PredictionCreationResult:
        """@brief 原子结算过期记录并创建预测 / Atomically settle an expired record and create a prediction.

        @param command 创建命令 / Creation command.
        @param quote 事务外获取的当前报价 / Current quote fetched outside the transaction.
        @return 创建结果 / Creation result.
        """

        ...

    async def has_due_prediction(self, *, now: datetime) -> bool:
        """@brief 检查是否存在到期预测 / Check whether a prediction is due.

        @param now 当前时间 / Current time.
        @return 有到期记录为 True / True when a due record exists.
        """

        ...

    async def settle_due_predictions(
        self,
        *,
        quote: PriceQuote,
        settled_at: datetime,
        limit: int,
    ) -> int:
        """@brief 原子结算并写入 durable 通知 / Atomically settle due predictions and enqueue durable notifications.

        @param quote 结算报价 / Settlement quote.
        @param settled_at 结算时间 / Settlement time.
        @param limit 最大批量 / Maximum batch size.
        @return 已结算数量 / Number settled.
        """

        ...


class SwapOperations(Protocol):
    """@brief FOGMOE 兑换查询与原子提交端口 / FOGMOE swap query and atomic-submission port."""

    async def pending_swap(self, user_id: int) -> TokenSwapRequest | None:
        """@brief 读取待处理兑换 / Read a pending token swap.

        @param user_id 用户 ID / User ID.
        @return 待处理请求或 None / Pending request or None.
        """

        ...

    async def submit_swap(self, command: SubmitTokenSwap) -> SwapSubmissionResult:
        """@brief 原子扣费并创建幂等兑换请求 / Atomically charge and create an idempotent swap request.

        @param command 兑换命令 / Swap command.
        @return 提交结果 / Submission result.
        """

        ...


__all__ = [
    "BtcPriceSource",
    "ChartOperations",
    "CryptoAccountReader",
    "PredictionOperations",
    "SwapOperations",
]
