"""@brief 与 Telegram、SQL 和交易所 SDK 无关的 Crypto 领域模型 / Crypto domain models independent of Telegram, SQL, and exchange SDKs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import re


BTC_PREDICTION_MINIMUM = 20
"""@brief BTC 预测最低投入金币 / Minimum coin stake for a BTC prediction."""

BTC_PREDICTION_DURATION = timedelta(minutes=10)
"""@brief BTC 预测的固定窗口 / Fixed BTC prediction window."""

SWAP_MINIMUM = 10_000
"""@brief FOGMOE 兑换最低金币数 / Minimum coin amount for a FOGMOE swap."""

_CONTRACT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,100}$")
"""@brief 当前支持链的保守合约地址字符集 / Conservative contract-address alphabet for supported chains."""

_SOLANA_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")
"""@brief 兼容旧命令的 Solana Base58 地址格式 / Legacy-compatible Solana Base58 address format."""


class Blockchain(StrEnum):
    """@brief 图表功能支持的区块链 / Blockchains supported by the chart feature."""

    SOLANA = "sol"
    """@brief Solana / Solana."""

    ETHEREUM = "eth"
    """@brief Ethereum / Ethereum."""

    BLAST = "blast"
    """@brief Blast / Blast."""

    BSC = "bsc"
    """@brief BNB Smart Chain / BNB Smart Chain."""

    @classmethod
    def parse(cls, value: str) -> Blockchain:
        """@brief 解析旧命令接受的链别名 / Parse chain aliases accepted by the legacy command.

        @param value 用户输入的链名称 / User-supplied chain name.
        @return 规范链值 / Canonical blockchain value.
        @raise ValueError 链名称不受支持时抛出 / Raised for an unsupported chain name.
        """

        normalized = value.strip().lower()
        aliases = {
            "sol": cls.SOLANA,
            "solana": cls.SOLANA,
            "eth": cls.ETHEREUM,
            "ethereum": cls.ETHEREUM,
            "blast": cls.BLAST,
            "bsc": cls.BSC,
            "bnb": cls.BSC,
        }
        try:
            return aliases[normalized]
        except KeyError as error:
            raise ValueError(f"Unsupported blockchain: {value!r}") from error

    @property
    def display_name(self) -> str:
        """@brief 返回用户可见链名称 / Return the user-visible chain name.

        @return 展示名称 / Display name.
        """

        return {
            Blockchain.SOLANA: "Solana",
            Blockchain.ETHEREUM: "Ethereum",
            Blockchain.BLAST: "Blast",
            Blockchain.BSC: "BSC",
        }[self]


@dataclass(frozen=True, slots=True, order=True)
class ContractAddress:
    """@brief 已规范化的图表合约地址 / Normalized chart contract address.

    @param value 链上合约地址文本 / On-chain contract address text.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验合约地址 / Validate the contract address.

        @return None / None.
        @raise ValueError 地址为空、过长或包含路径控制字符时抛出 / Raised for an empty, oversized, or path-controlling address.
        """

        normalized = self.value.strip()
        if _CONTRACT_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "Contract address must contain 1-100 alphanumeric characters"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """@brief 返回规范地址 / Return the canonical address.

        @return 地址文本 / Address text.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class ChartToken:
    """@brief 群组绑定的图表代币 / Chart token bound to a group.

    @param chain 规范链 / Canonical blockchain.
    @param contract 合约地址 / Contract address.
    """

    chain: Blockchain
    contract: ContractAddress


@dataclass(frozen=True, slots=True, order=True)
class CoinStake:
    """@brief 正整数金币投入 / Positive integer coin stake.

    @param value 金币数量 / Coin amount.
    """

    value: int

    def __post_init__(self) -> None:
        """@brief 校验金币数量 / Validate the coin amount.

        @return None / None.
        @raise ValueError 金币非正时抛出 / Raised when the amount is not positive.
        """

        if isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("Coin stake must be a positive integer")

    def __int__(self) -> int:
        """@brief 返回整数金币数 / Return the integer coin amount.

        @return 金币数量 / Coin amount.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class PriceQuote:
    """@brief 正数 BTC/USDT 报价 / Positive BTC/USDT quote.

    @param value 精确十进制价格 / Exact decimal price.
    """

    value: Decimal

    def __post_init__(self) -> None:
        """@brief 校验价格为有限正数 / Validate that the quote is finite and positive.

        @return None / None.
        @raise ValueError 价格非有限或非正时抛出 / Raised when the quote is non-finite or non-positive.
        """

        if not self.value.is_finite() or self.value <= 0:
            raise ValueError("Price quote must be finite and positive")


class PredictionDirection(StrEnum):
    """@brief BTC 预测方向 / BTC prediction direction."""

    UP = "up"
    """@brief 预测上涨 / Predict an increase."""

    DOWN = "down"
    """@brief 预测下跌或持平 / Predict a decrease or no increase."""


@dataclass(frozen=True, slots=True, order=True)
class SolanaWalletAddress:
    """@brief 兼容既有产品规则的 Solana 钱包地址 / Solana wallet address compatible with the existing product rule.

    @param value Base58 地址文本 / Base58 address text.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验旧产品接受的 43/44 字符 Base58 格式 / Validate the legacy 43/44-character Base58 format.

        @return None / None.
        @raise ValueError 地址格式非法时抛出 / Raised for an invalid address format.
        @note 这是语法校验而非链上账户存在性证明 / This is syntax validation, not proof that an on-chain account exists.
        """

        normalized = self.value.strip()
        if _SOLANA_PATTERN.fullmatch(normalized) is None:
            raise ValueError("Invalid Solana wallet address")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """@brief 返回规范地址 / Return the canonical address.

        @return 地址文本 / Address text.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class PredictionOutcome:
    """@brief 已结算 BTC 预测 / Settled BTC prediction.

    @param request_key 原始预测幂等键 / Original prediction idempotency key.
    @param user_id 用户 ID / User ID.
    @param chat_id 结果投递 chat ID / Result-delivery chat ID.
    @param direction 预测方向 / Predicted direction.
    @param amount 投入金币 / Coin stake.
    @param start_price 起始价格 / Starting price.
    @param end_price 结算价格 / Settlement price.
    @param started_at 开始时间 / Start time.
    @param due_at 到期时间 / Due time.
    @param settled_at 实际结算时间 / Actual settlement time.
    @param correct 是否命中 / Whether the prediction was correct.
    @param reward 返还本金与奖励总数；失败为零 / Principal plus reward on success; zero on failure.
    """

    request_key: str
    user_id: int
    chat_id: int
    direction: PredictionDirection
    amount: CoinStake
    start_price: PriceQuote
    end_price: PriceQuote
    started_at: datetime
    due_at: datetime
    settled_at: datetime
    correct: bool
    reward: int

    def __post_init__(self) -> None:
        """@brief 校验结算快照不变量 / Validate settlement snapshot invariants.

        @return None / None.
        @raise ValueError 标识、时间或奖励非法时抛出 / Raised for invalid identifiers, timing, or reward.
        """

        key = self.request_key.strip()
        if not key or len(key) > 512:
            raise ValueError("Prediction request key must contain 1-512 characters")
        if self.user_id <= 0 or self.chat_id == 0:
            raise ValueError("Prediction user and chat identifiers must be valid")
        started_at = _utc(self.started_at)
        due_at = _utc(self.due_at)
        settled_at = _utc(self.settled_at)
        if due_at <= started_at or settled_at < due_at:
            raise ValueError("Prediction settlement times are inconsistent")
        if self.reward < 0 or (not self.correct and self.reward != 0):
            raise ValueError("Prediction reward is inconsistent with its outcome")
        object.__setattr__(self, "request_key", key)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "settled_at", settled_at)

    @property
    def change_percent(self) -> Decimal:
        """@brief 返回价格变化百分比 / Return the percentage price change.

        @return 带符号的百分比 / Signed percentage.
        """

        return (
            (self.end_price.value - self.start_price.value)
            / self.start_price.value
            * Decimal(100)
        )


def calculate_prediction_outcome(
    *,
    request_key: str,
    user_id: int,
    chat_id: int,
    direction: PredictionDirection,
    amount: CoinStake,
    start_price: PriceQuote,
    end_price: PriceQuote,
    started_at: datetime,
    due_at: datetime,
    settled_at: datetime,
) -> PredictionOutcome:
    """@brief 按旧规则结算预测 / Settle a prediction using the legacy product rule.

    @param request_key 原始预测幂等键 / Original prediction idempotency key.
    @param user_id 用户 ID / User ID.
    @param chat_id 投递目标 / Delivery target.
    @param direction 预测方向 / Predicted direction.
    @param amount 投入金币 / Coin stake.
    @param start_price 起始报价 / Starting quote.
    @param end_price 结束报价 / Ending quote.
    @param started_at 开始时间 / Start time.
    @param due_at 到期时间 / Due time.
    @param settled_at 结算时间 / Settlement time.
    @return 不可变结算结果 / Immutable settlement result.
    @note 与旧实现一致，持平不算上涨，因此 ``down`` 在持平时命中 / As in the legacy implementation, a tie is not an increase, so ``down`` wins on a tie.
    """

    increased = end_price.value > start_price.value
    correct = (direction is PredictionDirection.UP and increased) or (
        direction is PredictionDirection.DOWN and not increased
    )
    reward = int(amount) * 18 // 10 if correct else 0
    return PredictionOutcome(
        request_key=request_key,
        user_id=user_id,
        chat_id=chat_id,
        direction=direction,
        amount=amount,
        start_price=start_price,
        end_price=end_price,
        started_at=started_at,
        due_at=due_at,
        settled_at=settled_at,
        correct=correct,
        reward=reward,
    )


def _utc(value: datetime) -> datetime:
    """@brief 将时间规范为 UTC aware datetime / Normalize a datetime to UTC-aware form.

    @param value 输入时间 / Input datetime.
    @return UTC 时间 / UTC datetime.
    @note 旧数据库 ``TIMESTAMP`` 值按 UTC 解释 / Legacy database ``TIMESTAMP`` values are interpreted as UTC.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
