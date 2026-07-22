"""@brief 群组代币图表领域模型 / Group token-chart domain models.

当前 crypto 边界只描述群组展示用的链与合约地址；它不再定义钱包、报价、预测、兑换或
任何资产价值语义。
/ The current crypto boundary describes only a chain and contract address for group display.  It
no longer defines wallets, price quotes, predictions, swaps, or any asset-value semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_CONTRACT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,100}$")
"""@brief 支持链的保守合约地址字符集 / Conservative contract-address alphabet for supported chains."""


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
        """@brief 解析图表命令接受的链别名 / Parse chain aliases accepted by the chart command.

        @param value 用户输入的链名称 / User-supplied chain name.
        @return 规范链值 / Canonical blockchain value.
        @raise ValueError 链名称不受支持时抛出 / Raised for an unsupported chain name.
        """

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
            return aliases[value.strip().casefold()]
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
        """@brief 校验并规范合约地址 / Validate and normalize the contract address.

        @return None / None.
        @raise ValueError 地址为空、过长或包含不支持字符时抛出 / Raised for an empty, oversized, or unsupported address.
        """

        normalized = self.value.strip()
        if _CONTRACT_PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "Contract address must contain 1-100 alphanumeric characters"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """@brief 返回规范地址文本 / Return the canonical address text.

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


__all__ = ["Blockchain", "ChartToken", "ContractAddress"]
