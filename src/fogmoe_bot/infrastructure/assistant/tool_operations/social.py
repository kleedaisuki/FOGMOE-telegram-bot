"""@brief Assistant 善意赠币原子 mutation / Assistant kindness-gift atomic mutation."""

import random
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.scheduling import ensure_utc, to_storage_datetime
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import user_repository

from .parsing import bounded_int, iso_instant


async def execute_kindness_gift(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection,
) -> JsonValue:
    """在 receipt transaction 中执行 24h kindness gift。"""

    account = await user_repository.fetch_user_account(
        request.context.user_id,
        connection=connection,
        for_update=True,
    )
    if account is None:
        return {"error": "Recipient user not found"}
    latest = await db_connection.fetch_one(
        "SELECT amount, created_at FROM economy.kindness_gifts WHERE recipient_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
        (request.context.user_id,),
        connection=connection,
    )
    now = datetime.now(UTC)
    if latest is not None and now - ensure_utc(cast(datetime, latest[1])) < timedelta(
        hours=24
    ):
        return {
            "status": "cooldown",
            "last_amount": int(latest[0]),
            "last_time": iso_instant(latest[1]),
        }
    raw_amount = request.arguments.get("amount")
    amount = (
        random.randint(1, 10)
        if raw_amount is None
        else bounded_int(request.arguments, "amount", minimum=1, maximum=10)
    )
    await user_repository.add_free_coins(
        request.context.user_id,
        amount,
        connection=connection,
    )
    await db_connection.execute(
        "INSERT INTO economy.kindness_gifts (recipient_id, amount, created_at) VALUES (%s, %s, %s)",
        (request.context.user_id, amount, to_storage_datetime(now)),
        connection=connection,
    )
    return {
        "status": "granted",
        "recipient_id": request.context.user_id,
        "amount": amount,
        "recipient_coins_before": account.total_coins,
        "recipient_coins_after": account.total_coins + amount,
    }
