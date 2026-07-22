"""@brief 群组代币图表应用服务 / Group token-chart application service.

这是原 Crypto 聚合中唯一仍属当前产品边界的能力：群组管理员绑定一个链与合约地址，
供 `/chart` 查询展示。它不读取价格、不管理钱包，也不承载任何下注、兑换或资产价值
逻辑。
/ This is the sole capability from the former Crypto aggregate that remains in the current
product boundary: a group administrator binds one chain and contract address for `/chart` display.
It neither reads prices nor manages wallets, wagers, swaps, or asset-value logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.crypto import ChartToken

CHART_SERVICE_DATA_KEY = "fogmoe.chart.service"
"""@brief ``bot_data`` 中图表服务的稳定键 / Stable chart-service key in ``bot_data``."""


@dataclass(frozen=True, slots=True)
class BindChartToken:
    """@brief 绑定群组图表代币命令 / Bind-group-chart-token command.

    @param group_id 群组 ID / Group identifier.
    @param actor_id 发起绑定的管理员 ID / Administrator initiating the binding.
    @param token 规范化链与合约地址 / Canonical chain and contract address.
    @param idempotency_key 来源 Update 的幂等键 / Source-Update idempotency key.
    """

    group_id: int
    actor_id: int
    token: ChartToken
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验身份和幂等键 / Validate identifiers and idempotency key.

        @return None / None.
        @raise ValueError 标识或幂等键非法时抛出 / Raised for invalid identifiers or idempotency key.
        """

        if self.group_id == 0 or self.actor_id <= 0:
            raise ValueError("Chart binding identifiers are invalid")
        object.__setattr__(
            self, "idempotency_key", _idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class ClearChartToken:
    """@brief 清除群组图表代币命令 / Clear-group-chart-token command.

    @param group_id 群组 ID / Group identifier.
    @param actor_id 发起清除的管理员 ID / Administrator initiating the clear.
    @param idempotency_key 来源 Update 的幂等键 / Source-Update idempotency key.
    """

    group_id: int
    actor_id: int
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验身份和幂等键 / Validate identifiers and idempotency key.

        @return None / None.
        @raise ValueError 标识或幂等键非法时抛出 / Raised for invalid identifiers or idempotency key.
        """

        if self.group_id == 0 or self.actor_id <= 0:
            raise ValueError("Chart clear identifiers are invalid")
        object.__setattr__(
            self, "idempotency_key", _idempotency_key(self.idempotency_key)
        )


@dataclass(frozen=True, slots=True)
class ChartMutationResult:
    """@brief 图表绑定写操作的幂等结果 / Idempotent result of a chart-binding mutation.

    @param token 绑定后的代币；清除后为 None / Token after binding, or None after clearing.
    @param replayed 是否来自同一幂等键的回放 / Whether returned from an idempotent replay.
    """

    token: ChartToken | None
    replayed: bool = False


class ChartOperations(Protocol):
    """@brief 群组图表绑定的原子持久化端口 / Atomic persistence port for group chart bindings."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 读取群组当前图表绑定 / Read a group's current chart binding.

        @param group_id 群组 ID / Group identifier.
        @return 已绑定代币或 None / Bound token or None.
        """

        ...

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 幂等写入图表绑定 / Idempotently write a chart binding.

        @param command 图表绑定命令 / Chart-binding command.
        @return 写入结果 / Mutation result.
        """

        ...

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 幂等清除图表绑定 / Idempotently clear a chart binding.

        @param command 图表清除命令 / Chart-clear command.
        @return 清除结果 / Mutation result.
        """

        ...


class ChartService:
    """@brief 无 Telegram、报价与银行依赖的图表用例 / Chart use cases without Telegram, pricing, or bank dependencies."""

    def __init__(self, operations: ChartOperations) -> None:
        """@brief 注入唯一图表持久化端口 / Inject the sole chart-persistence port.

        @param operations 图表绑定原子端口 / Atomic chart-binding port.
        @return None / None.
        """

        self._operations = operations
        """@brief 图表持久化端口 / Chart persistence port."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 读取群组图表代币 / Read a group's chart token.

        @param group_id 群组 ID / Group identifier.
        @return 已绑定代币或 None / Bound token or None.
        """

        if group_id == 0:
            raise ValueError("Chart group ID cannot be zero")
        return await self._operations.chart_token(group_id)

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 绑定图表代币 / Bind a chart token.

        @param command 已校验绑定命令 / Validated binding command.
        @return 幂等写入结果 / Idempotent mutation result.
        """

        return await self._operations.bind_chart(command)

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 清除图表代币 / Clear a chart token.

        @param command 已校验清除命令 / Validated clear command.
        @return 幂等清除结果 / Idempotent mutation result.
        """

        return await self._operations.clear_chart(command)


def _idempotency_key(value: str) -> str:
    """@brief 规范并校验图表命令幂等键 / Normalize and validate a chart-command idempotency key.

    @param value 原始幂等键 / Raw idempotency key.
    @return 规范化幂等键 / Normalized idempotency key.
    @raise ValueError 键为空或过长时抛出 / Raised when the key is blank or too long.
    """

    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("Chart idempotency key must contain 1-200 characters")
    return normalized


__all__ = [
    "BindChartToken",
    "CHART_SERVICE_DATA_KEY",
    "ChartMutationResult",
    "ChartOperations",
    "ChartService",
    "ClearChartToken",
]
