"""@brief 群组代币图表绑定的 PostgreSQL 适配器 / PostgreSQL adapter for group chart-token bindings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fogmoe_bot.application.crypto.chart_service import (
    BindChartToken,
    ChartMutationResult,
    ClearChartToken,
)
from fogmoe_bot.domain.crypto import Blockchain, ChartToken, ContractAddress
from fogmoe_bot.infrastructure.database import connection as db_connection

from .chart_receipts import (
    advisory_lock,
    load_chart_receipt,
    lock_chart_receipt,
    save_chart_receipt,
)


class PostgresChartOperations:
    """@brief 原子、幂等地维护群组图表绑定 / Atomically and idempotently maintain group chart bindings."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """@brief 读取群组当前图表绑定 / Read a group's current chart binding.

        @param group_id 群组 ID / Group identifier.
        @return 已绑定代币或 None / Bound token or None.
        """

        row = await db_connection.fetch_one(
            "SELECT chain, ca FROM crypto.group_chart_tokens WHERE group_id = %s",
            (group_id,),
        )
        return _chart_token(row) if row is not None else None

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """@brief 在群组 advisory lock 下幂等绑定代币 / Idempotently bind a token under the group advisory lock.

        @param command 已校验的图表绑定命令 / Validated chart-binding command.
        @return 绑定或幂等回放结果 / Binding or idempotent replay result.
        @note 此事务只触及 ``crypto.group_chart_tokens`` 与 chart receipt；绝不访问
            账户余额。/ This transaction touches only ``crypto.group_chart_tokens`` and
            chart receipts; it never accesses an account balance.
        """

        async with db_connection.transaction() as connection:
            await lock_chart_receipt(command.idempotency_key, connection)
            replay = await load_chart_receipt(
                command.idempotency_key,
                operation_kind="chart.bind",
                actor_id=command.actor_id,
                connection=connection,
            )
            if replay is not None:
                token = _token_from_mapping(replay.get("token"))
                return ChartMutationResult(token, replayed=True)
            await advisory_lock(f"crypto-chart:{command.group_id}", connection)
            await db_connection.execute(
                "INSERT INTO crypto.group_chart_tokens "
                "(group_id, chain, ca, set_by) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (group_id) DO UPDATE SET chain = EXCLUDED.chain, "
                "ca = EXCLUDED.ca, set_by = EXCLUDED.set_by, "
                "updated_at = CURRENT_TIMESTAMP",
                (
                    command.group_id,
                    command.token.chain.value,
                    str(command.token.contract),
                    command.actor_id,
                ),
                connection=connection,
            )
            result = ChartMutationResult(command.token)
            await save_chart_receipt(
                command.idempotency_key,
                "chart.bind",
                command.actor_id,
                _chart_result_mapping(result),
                connection,
            )
            return result

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """@brief 在群组 advisory lock 下幂等清除绑定 / Idempotently clear a binding under the group advisory lock.

        @param command 已校验的图表清除命令 / Validated chart-clear command.
        @return 清除或幂等回放结果 / Clear or idempotent replay result.
        """

        async with db_connection.transaction() as connection:
            await lock_chart_receipt(command.idempotency_key, connection)
            replay = await load_chart_receipt(
                command.idempotency_key,
                operation_kind="chart.clear",
                actor_id=command.actor_id,
                connection=connection,
            )
            if replay is not None:
                return ChartMutationResult(None, replayed=True)
            await advisory_lock(f"crypto-chart:{command.group_id}", connection)
            await db_connection.execute(
                "DELETE FROM crypto.group_chart_tokens WHERE group_id = %s",
                (command.group_id,),
                connection=connection,
            )
            result = ChartMutationResult(None)
            await save_chart_receipt(
                command.idempotency_key,
                "chart.clear",
                command.actor_id,
                _chart_result_mapping(result),
                connection,
            )
            return result


def _chart_token(row: Sequence[object]) -> ChartToken:
    """@brief 将 ``(chain, contract)`` 行映射成图表代币 / Map a ``(chain, contract)`` row into a chart token.

    @param row 数据库行 / Database row.
    @return 规范图表代币 / Canonical chart token.
    """

    return ChartToken(Blockchain(str(row[0])), ContractAddress(str(row[1])))


def _token_from_mapping(value: object) -> ChartToken | None:
    """@brief 从幂等 receipt 恢复图表代币 / Restore a chart token from an idempotency receipt.

    @param value receipt 内原始 token 值 / Raw token value in a receipt.
    @return 恢复后的代币或 None / Restored token or None.
    @raise ValueError receipt 形状非法时抛出 / Raised for an invalid receipt shape.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Invalid chart token receipt")
    return ChartToken(
        Blockchain(str(value["chain"])),
        ContractAddress(str(value["contract"])),
    )


def _chart_result_mapping(result: ChartMutationResult) -> dict[str, object]:
    """@brief 将图表变更结果序列化为幂等 receipt / Serialize a chart mutation result for an idempotency receipt.

    @param result 图表变更结果 / Chart mutation result.
    @return JSON 兼容映射 / JSON-compatible mapping.
    """

    token = result.token
    return {
        "token": (
            {"chain": token.chain.value, "contract": str(token.contract)}
            if token is not None
            else None
        )
    }
