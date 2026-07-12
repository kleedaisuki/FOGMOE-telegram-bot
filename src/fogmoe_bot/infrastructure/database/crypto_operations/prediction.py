"""PostgreSQL adapter for durable BTC prediction creation and settlement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.crypto.workflow import (
    ActivePrediction,
    CreateBtcPrediction,
    CryptoResultCode,
    PredictionCreationResult,
    render_prediction_outcome,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.domain.crypto import (
    CoinStake,
    PredictionDirection,
    PredictionOutcome,
    PriceQuote,
    calculate_prediction_outcome,
)
from fogmoe_bot.domain.economy import AccountBalance
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)

from .common import (
    LockedAccount,
    aware_timestamp,
    db_timestamp,
    integer,
    load_receipt,
    lock_account,
    lock_receipt,
    save_receipt,
    write_balance,
)


_PREDICTION_COLUMNS = (
    "request_key, user_id, chat_id, predict_type, amount, start_price, "
    "start_time, end_time, is_completed"
)


class PostgresPredictionOperations:
    """Own account-first BTC prediction creation and settlement transactions."""

    def __init__(
        self,
        *,
        admin_user_id: int,
        outbox: StandaloneOutboxWriter | None = None,
    ) -> None:
        """Inject the administrator identity and connection-bound outbox writer."""

        self._admin_user_id = admin_user_id
        self._outbox = outbox or PostgresOutboxRepository()

    async def active_prediction(
        self,
        user_id: int,
        *,
        now: datetime,
    ) -> ActivePrediction | None:
        """Read an unexpired prediction for one user."""

        row = await db_connection.fetch_one(
            "SELECT predict_type, amount, start_price, start_time, end_time "
            "FROM crypto.user_btc_predictions "
            "WHERE user_id = %s AND is_completed = FALSE AND end_time > %s",
            (user_id, db_timestamp(now)),
        )
        return _active_prediction(row) if row is not None else None

    async def create_prediction(
        self,
        command: CreateBtcPrediction,
        *,
        quote: PriceQuote,
    ) -> PredictionCreationResult:
        """Settle expired state, charge, and create under one account-first transaction."""

        async with db_connection.transaction() as connection:
            account = await lock_account(command.user_id, connection)
            await lock_receipt(command.idempotency_key, connection)
            replay = await load_receipt(
                command.idempotency_key,
                operation_kind="prediction.create",
                actor_id=command.user_id,
                connection=connection,
            )
            if replay is not None:
                return _prediction_result_from_mapping(replay, replayed=True)
            if account is None:
                result = PredictionCreationResult(CryptoResultCode.NOT_REGISTERED)
                await _save_prediction_result(command, result, connection)
                return result
            existing = await db_connection.fetch_one(
                f"SELECT {_PREDICTION_COLUMNS} "
                "FROM crypto.user_btc_predictions WHERE user_id = %s FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            if existing is not None and not bool(existing[8]):
                due_at = aware_timestamp(cast(datetime, existing[7]))
                if due_at > command.requested_at:
                    active = _active_prediction(
                        (
                            existing[3],
                            existing[4],
                            existing[5],
                            existing[6],
                            existing[7],
                        )
                    )
                    result = PredictionCreationResult(
                        CryptoResultCode.ACTIVE_PREDICTION,
                        prediction=active,
                        balance=account.balance.total,
                    )
                    await _save_prediction_result(command, result, connection)
                    return result
                account = await self._settle_locked_prediction(
                    existing,
                    account=account,
                    quote=quote,
                    settled_at=command.requested_at,
                    connection=connection,
                )
            charged = account.balance.spend(int(command.amount))
            if charged is None:
                result = PredictionCreationResult(
                    CryptoResultCode.INSUFFICIENT_COINS,
                    balance=account.balance.total,
                )
                await _save_prediction_result(command, result, connection)
                return result
            await write_balance(charged, self._admin_user_id, connection)
            await db_connection.execute(
                "DELETE FROM crypto.user_btc_predictions WHERE user_id = %s",
                (command.user_id,),
                connection=connection,
            )
            await db_connection.execute(
                "INSERT INTO crypto.user_btc_predictions "
                "(user_id, predict_type, amount, start_price, start_time, end_time, "
                "request_key, chat_id, is_completed) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE)",
                (
                    command.user_id,
                    command.direction.value,
                    int(command.amount),
                    quote.value,
                    db_timestamp(command.requested_at),
                    db_timestamp(command.due_at),
                    command.idempotency_key,
                    command.chat_id,
                ),
                connection=connection,
            )
            prediction = ActivePrediction(
                command.direction,
                command.amount,
                quote,
                command.requested_at,
                command.due_at,
            )
            result = PredictionCreationResult(
                CryptoResultCode.SUCCESS,
                prediction=prediction,
                balance=charged.total,
            )
            await _save_prediction_result(command, result, connection)
            return result

    async def has_due_prediction(self, *, now: datetime) -> bool:
        """Check for one due prediction whose account still exists."""

        row = await db_connection.fetch_one(
            "SELECT 1 FROM crypto.user_btc_predictions AS prediction "
            "JOIN identity.users AS account ON account.id = prediction.user_id "
            "WHERE prediction.is_completed = FALSE AND prediction.end_time <= %s "
            "LIMIT 1",
            (db_timestamp(now),),
        )
        return row is not None

    async def settle_due_predictions(
        self,
        *,
        quote: PriceQuote,
        settled_at: datetime,
        limit: int,
    ) -> int:
        """Settle an account-ID-ordered, ``SKIP LOCKED`` batch into the outbox."""

        if limit < 1:
            raise ValueError("Settlement limit must be positive")
        async with db_connection.transaction() as connection:
            account_rows = await db_connection.fetch_all(
                "SELECT account.id, account.coins, account.coins_paid, "
                "account.user_plan, account.name FROM identity.users AS account "
                "WHERE EXISTS (SELECT 1 FROM crypto.user_btc_predictions AS prediction "
                "WHERE prediction.user_id = account.id "
                "AND prediction.is_completed = FALSE AND prediction.end_time <= %s) "
                "ORDER BY account.id LIMIT %s FOR UPDATE OF account SKIP LOCKED",
                (db_timestamp(settled_at), limit),
                connection=connection,
            )
            accounts = {
                int(row[0]): LockedAccount(
                    AccountBalance(
                        int(row[0]),
                        int(row[1]),
                        int(row[2]),
                        str(row[3]),
                    ),
                    str(row[4]),
                )
                for row in account_rows
            }
            user_ids = tuple(accounts)
            if not user_ids:
                return 0
            placeholders = ", ".join("%s" for _ in user_ids)
            prediction_rows = await db_connection.fetch_all(
                f"SELECT {_PREDICTION_COLUMNS} "
                "FROM crypto.user_btc_predictions "
                f"WHERE user_id IN ({placeholders}) AND is_completed = FALSE "
                "AND end_time <= %s ORDER BY user_id FOR UPDATE SKIP LOCKED",
                (*user_ids, db_timestamp(settled_at)),
                connection=connection,
            )
            settled = 0
            for row in prediction_rows:
                user_id = int(row[1])
                account = accounts.get(user_id)
                if account is None:
                    continue
                await self._settle_locked_prediction(
                    row,
                    account=account,
                    quote=quote,
                    settled_at=settled_at,
                    connection=connection,
                )
                settled += 1
            return settled

    async def _settle_locked_prediction(
        self,
        row: Sequence[object],
        *,
        account: LockedAccount,
        quote: PriceQuote,
        settled_at: datetime,
        connection: AsyncConnection,
    ) -> LockedAccount:
        """Commit outcome, reward, and outbox for an already-locked prediction."""

        outcome = _prediction_outcome(row, quote=quote, settled_at=settled_at)
        balance = account.balance
        if outcome.reward:
            balance = AccountBalance(
                balance.user_id,
                balance.free + outcome.reward,
                balance.paid,
                balance.plan,
            )
            await write_balance(balance, self._admin_user_id, connection)
        changed = await db_connection.execute(
            "UPDATE crypto.user_btc_predictions SET is_completed = TRUE, "
            "end_price = %s, is_correct = %s, reward = %s, settled_at = %s "
            "WHERE user_id = %s AND request_key = %s AND is_completed = FALSE",
            (
                outcome.end_price.value,
                outcome.correct,
                outcome.reward,
                outcome.settled_at,
                outcome.user_id,
                outcome.request_key,
            ),
            connection=connection,
        )
        if changed != 1:
            raise RuntimeError("Locked prediction could not be settled")
        await self._enqueue_prediction_result(
            outcome,
            display_name=account.display_name,
            connection=connection,
        )
        return LockedAccount(balance, account.display_name)

    async def _enqueue_prediction_result(
        self,
        outcome: PredictionOutcome,
        *,
        display_name: str,
        connection: AsyncConnection,
    ) -> None:
        """Enqueue the deterministic result notification in the settlement transaction."""

        conversation_id = ConversationId(f"crypto:prediction:user:{outcome.user_id}")
        digest = sha256(outcome.request_key.encode("utf-8")).hexdigest()
        idempotency_key = f"crypto:prediction-result:{digest}"
        draft = OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                conversation_id,
                idempotency_key,
            ),
            conversation_id=conversation_id,
            turn_id=None,
            delivery_stream_id=DeliveryStreamId(
                f"telegram:primary:chat:{outcome.chat_id}:thread:0"
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={
                "chat_id": outcome.chat_id,
                "text": render_prediction_outcome(
                    outcome,
                    display_name=display_name,
                ),
                "disable_web_page_preview": False,
            },
            idempotency_key=idempotency_key,
            created_at=outcome.settled_at,
        )
        await self._outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            draft,
        )


def _active_prediction(row: Sequence[object]) -> ActivePrediction:
    """Map an active-prediction database row."""

    return ActivePrediction(
        PredictionDirection(str(row[0])),
        CoinStake(integer(row[1])),
        PriceQuote(Decimal(str(row[2]))),
        aware_timestamp(cast(datetime, row[3])),
        aware_timestamp(cast(datetime, row[4])),
    )


def _prediction_outcome(
    row: Sequence[object],
    *,
    quote: PriceQuote,
    settled_at: datetime,
) -> PredictionOutcome:
    """Calculate a settlement from a locked database row."""

    return calculate_prediction_outcome(
        request_key=str(row[0]),
        user_id=integer(row[1]),
        chat_id=integer(row[2]),
        direction=PredictionDirection(str(row[3])),
        amount=CoinStake(integer(row[4])),
        start_price=PriceQuote(Decimal(str(row[5]))),
        end_price=quote,
        started_at=aware_timestamp(cast(datetime, row[6])),
        due_at=aware_timestamp(cast(datetime, row[7])),
        settled_at=settled_at,
    )


def _prediction_mapping(result: PredictionCreationResult) -> dict[str, object]:
    """Serialize a prediction-creation result for an idempotency receipt."""

    prediction = result.prediction
    return {
        "code": result.code.value,
        "balance": result.balance,
        "prediction": (
            {
                "direction": prediction.direction.value,
                "amount": int(prediction.amount),
                "start_price": str(prediction.start_price.value),
                "started_at": prediction.started_at.isoformat(),
                "due_at": prediction.due_at.isoformat(),
            }
            if prediction is not None
            else None
        ),
    }


def _prediction_result_from_mapping(
    value: Mapping[str, Any],
    *,
    replayed: bool,
) -> PredictionCreationResult:
    """Restore a prediction-creation result from an idempotency receipt."""

    prediction_value = value.get("prediction")
    prediction: ActivePrediction | None = None
    if isinstance(prediction_value, Mapping):
        prediction = ActivePrediction(
            PredictionDirection(str(prediction_value["direction"])),
            CoinStake(int(prediction_value["amount"])),
            PriceQuote(Decimal(str(prediction_value["start_price"]))),
            datetime.fromisoformat(str(prediction_value["started_at"])),
            datetime.fromisoformat(str(prediction_value["due_at"])),
        )
    return PredictionCreationResult(
        CryptoResultCode(str(value["code"])),
        prediction=prediction,
        balance=int(value.get("balance", 0)),
        replayed=replayed,
    )


async def _save_prediction_result(
    command: CreateBtcPrediction,
    result: PredictionCreationResult,
    connection: AsyncConnection,
) -> None:
    """Persist a prediction-creation receipt in the current transaction."""

    await save_receipt(
        command.idempotency_key,
        "prediction.create",
        command.user_id,
        _prediction_mapping(result),
        connection,
    )
