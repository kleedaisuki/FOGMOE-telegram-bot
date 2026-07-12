"""PostgreSQL adapter for group chart-token bindings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fogmoe_bot.application.crypto.workflow import (
    BindChartToken,
    ChartMutationResult,
    ClearChartToken,
)
from fogmoe_bot.domain.crypto import Blockchain, ChartToken, ContractAddress
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import advisory_lock, load_receipt, lock_receipt, save_receipt


class PostgresChartOperations:
    """Own atomic, idempotent group chart-binding mutations."""

    async def chart_token(self, group_id: int) -> ChartToken | None:
        """Read a group's current chart binding."""

        row = await db_connection.fetch_one(
            "SELECT chain, ca FROM crypto.group_chart_tokens WHERE group_id = %s",
            (group_id,),
        )
        return _chart_token(row) if row is not None else None

    async def bind_chart(self, command: BindChartToken) -> ChartMutationResult:
        """Idempotently bind a token under the group advisory lock."""

        async with db_connection.transaction() as connection:
            await lock_receipt(command.idempotency_key, connection)
            replay = await load_receipt(
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
            await save_receipt(
                command.idempotency_key,
                "chart.bind",
                command.actor_id,
                _chart_result_mapping(result),
                connection,
            )
            return result

    async def clear_chart(self, command: ClearChartToken) -> ChartMutationResult:
        """Idempotently clear a binding under the group advisory lock."""

        async with db_connection.transaction() as connection:
            await lock_receipt(command.idempotency_key, connection)
            replay = await load_receipt(
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
            await save_receipt(
                command.idempotency_key,
                "chart.clear",
                command.actor_id,
                _chart_result_mapping(result),
                connection,
            )
            return result


def _chart_token(row: Sequence[object]) -> ChartToken:
    """Map a ``(chain, contract)`` row into a chart token."""

    return ChartToken(Blockchain(str(row[0])), ContractAddress(str(row[1])))


def _token_from_mapping(value: object) -> ChartToken | None:
    """Restore a chart token from an idempotency receipt."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Invalid chart token receipt")
    return ChartToken(
        Blockchain(str(value["chain"])),
        ContractAddress(str(value["contract"])),
    )


def _chart_result_mapping(result: ChartMutationResult) -> dict[str, object]:
    """Serialize a chart mutation result for an idempotency receipt."""

    token = result.token
    return {
        "token": (
            {"chain": token.chain.value, "contract": str(token.contract)}
            if token is not None
            else None
        )
    }
