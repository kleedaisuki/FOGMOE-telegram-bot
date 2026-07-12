"""PostgreSQL adapter for FOGMOE token-swap requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.crypto.workflow import (
    CryptoResultCode,
    SubmitTokenSwap,
    SwapSubmissionResult,
    TokenSwapRequest,
)
from fogmoe_bot.domain.crypto import CoinStake, SolanaWalletAddress
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import (
    aware_timestamp,
    integer,
    load_receipt,
    lock_account,
    lock_receipt,
    save_receipt,
    write_balance,
)


class PostgresSwapOperations:
    """Own atomic charging, request creation, and swap receipts."""

    def __init__(self, *, admin_user_id: int) -> None:
        """Store the administrator identity used to derive account plans."""

        self._admin_user_id = admin_user_id

    async def pending_swap(self, user_id: int) -> TokenSwapRequest | None:
        """Read the canonical pending or manual-review request."""

        row = await db_connection.fetch_one(
            "SELECT id, amount, wallet_address, request_time "
            "FROM crypto.token_swap_requests "
            "WHERE user_id = %s AND status IN ('pending', 'manual_review') "
            "ORDER BY (status = 'pending') DESC, request_time DESC NULLS FIRST, "
            "id DESC LIMIT 1",
            (user_id,),
        )
        return _swap_request(row) if row is not None else None

    async def submit_swap(self, command: SubmitTokenSwap) -> SwapSubmissionResult:
        """Charge and create one idempotent request under the account lock."""

        async with db_connection.transaction() as connection:
            account = await lock_account(command.user_id, connection)
            await lock_receipt(command.idempotency_key, connection)
            replay = await load_receipt(
                command.idempotency_key,
                operation_kind="swap.submit",
                actor_id=command.user_id,
                connection=connection,
            )
            if replay is not None:
                return _swap_result_from_mapping(replay, replayed=True)
            if account is None:
                result = SwapSubmissionResult(CryptoResultCode.NOT_REGISTERED)
                await _save_swap_result(command, result, connection)
                return result
            pending_row = await db_connection.fetch_one(
                "SELECT id, amount, wallet_address, request_time "
                "FROM crypto.token_swap_requests "
                "WHERE user_id = %s AND status IN ('pending', 'manual_review') "
                "ORDER BY (status = 'pending') DESC, request_time DESC NULLS FIRST, "
                "id DESC LIMIT 1 FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            if pending_row is not None:
                result = SwapSubmissionResult(
                    CryptoResultCode.PENDING_SWAP,
                    request=_swap_request(pending_row),
                    balance=account.balance.total,
                )
                await _save_swap_result(command, result, connection)
                return result
            charged = account.balance.spend(int(command.amount))
            if charged is None:
                result = SwapSubmissionResult(
                    CryptoResultCode.INSUFFICIENT_COINS,
                    balance=account.balance.total,
                )
                await _save_swap_result(command, result, connection)
                return result
            await write_balance(charged, self._admin_user_id, connection)
            row = await db_connection.fetch_one(
                "INSERT INTO crypto.token_swap_requests "
                "(user_id, username, wallet_address, amount, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, amount, wallet_address, request_time",
                (
                    command.user_id,
                    command.username,
                    str(command.wallet),
                    int(command.amount),
                    command.idempotency_key,
                ),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Swap request insert returned no canonical row")
            result = SwapSubmissionResult(
                CryptoResultCode.SUCCESS,
                request=_swap_request(row),
                balance=charged.total,
            )
            await _save_swap_result(command, result, connection)
            return result


def _swap_request(row: Sequence[object]) -> TokenSwapRequest:
    """Map a token-swap database row."""

    return TokenSwapRequest(
        integer(row[0]),
        CoinStake(integer(row[1])),
        SolanaWalletAddress(str(row[2])),
        aware_timestamp(cast(datetime, row[3])),
    )


def _swap_mapping(result: SwapSubmissionResult) -> dict[str, object]:
    """Serialize a swap result for an idempotency receipt."""

    request = result.request
    return {
        "code": result.code.value,
        "balance": result.balance,
        "request": (
            {
                "request_id": request.request_id,
                "amount": int(request.amount),
                "wallet": str(request.wallet),
                "requested_at": request.requested_at.isoformat(),
            }
            if request is not None
            else None
        ),
    }


def _swap_result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> SwapSubmissionResult:
    """Restore a swap result from an idempotency receipt."""

    request_value = value.get("request")
    request: TokenSwapRequest | None = None
    if isinstance(request_value, Mapping):
        request = TokenSwapRequest(
            int(request_value["request_id"]),
            CoinStake(int(request_value["amount"])),
            SolanaWalletAddress(str(request_value["wallet"])),
            datetime.fromisoformat(str(request_value["requested_at"])),
        )
    return SwapSubmissionResult(
        CryptoResultCode(str(value["code"])),
        request=request,
        balance=int(value.get("balance", 0)),
        replayed=replayed,
    )


async def _save_swap_result(
    command: SubmitTokenSwap,
    result: SwapSubmissionResult,
    connection: AsyncConnection,
) -> None:
    """Persist a swap result receipt in the current transaction."""

    await save_receipt(
        command.idempotency_key,
        "swap.submit",
        command.user_id,
        _swap_mapping(result),
        connection,
    )
