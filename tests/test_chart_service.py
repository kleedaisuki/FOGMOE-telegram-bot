"""@brief 群组代币图表领域和应用服务测试 / Group token-chart domain and application-service tests."""

from __future__ import annotations

import asyncio

import pytest

from fogmoe_bot.application.crypto.chart_service import (
    BindChartToken,
    ChartMutationResult,
    ChartService,
    ClearChartToken,
)
from fogmoe_bot.domain.crypto import Blockchain, ChartToken, ContractAddress


class _Charts:
    """@brief 记录图表端口调用的内存替身 / In-memory double recording chart-port calls."""

    def __init__(self) -> None:
        """@brief 初始化空绑定 / Initialize an empty binding.

        @return None / None.
        """

        self.token: ChartToken | None = None
        """@brief 当前图表绑定 / Current chart binding."""
        self.bound: list[BindChartToken] = []
        """@brief 已收到绑定命令 / Received binding commands."""
        self.cleared: list[ClearChartToken] = []
        """@brief 已收到清除命令 / Received clear commands."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 返回当前绑定 / Return the current binding.

        @param group_id 群组 ID / Group identifier.
        @return 当前绑定 / Current binding.
        """

        assert group_id == -100
        return self.token

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 记录绑定并返回结果 / Record a binding and return its result.

        @param command 图表绑定命令 / Chart binding command.
        @return 写入结果 / Mutation result.
        """

        self.bound.append(command)
        self.token = command.token
        return ChartMutationResult(command.token)

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 记录清除并返回结果 / Record a clear and return its result.

        @param command 图表清除命令 / Chart clear command.
        @return 清除结果 / Mutation result.
        """

        self.cleared.append(command)
        self.token = None
        return ChartMutationResult(None)


def test_chart_domain_normalizes_only_chain_and_contract_address() -> None:
    """@brief 图表领域只保留链和合约地址 / The chart domain retains only a chain and contract address.

    @return None / None.
    """

    assert Blockchain.parse("Solana") is Blockchain.SOLANA
    assert Blockchain.parse("BNB") is Blockchain.BSC
    assert str(ContractAddress(" 0xABC123 ")) == "0xABC123"
    with pytest.raises(ValueError, match="Unsupported"):
        Blockchain.parse("bitcoin")
    with pytest.raises(ValueError):
        ContractAddress("../../admin")


def test_chart_service_forwards_only_chart_operations() -> None:
    """@brief 服务只转发图表读取、绑定与清除 / Service forwards only chart read, bind, and clear operations.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行图表读写场景 / Run the chart read/write scenario.

        @return None / None.
        """

        charts = _Charts()
        service = ChartService(charts)
        token = ChartToken(Blockchain.SOLANA, ContractAddress("2z9nPFtF"))
        bind = BindChartToken(-100, 42, token, " chart:bind:1 ")

        assert await service.chart_token(-100) is None
        assert (await service.bind_chart(bind)).token == token
        assert await service.chart_token(-100) == token
        assert (
            await service.clear_chart(ClearChartToken(-100, 42, "chart:clear:1"))
        ).token is None
        assert charts.bound == [bind]
        assert len(charts.cleared) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ("", " ", "x" * 513))
def test_chart_commands_reject_invalid_idempotency_keys(key: str) -> None:
    """@brief 图表命令在边界拒绝空或过长幂等键 / Chart commands reject blank or oversized idempotency keys at the boundary.

    @param key 待验证幂等键 / Idempotency key to validate.
    @return None / None.
    """

    token = ChartToken(Blockchain.ETHEREUM, ContractAddress("0xABC123"))
    with pytest.raises(ValueError, match="idempotency"):
        BindChartToken(-100, 42, token, key)
