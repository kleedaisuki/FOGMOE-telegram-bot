"""@brief 多人奖池 PostgreSQL adapter / PostgreSQL adapter for multiplayer gamble pools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.gamble.models import (
    GambleCode,
    GambleResult,
    GambleSettlement,
    OpenGamble,
    PlaceGambleBet,
    SettleGamble,
)
from fogmoe_bot.application.games.ports.gamble import GambleOperations
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    EDIT_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.domain.games import (
    GambleBet,
    GambleSession,
    GameSessionId,
    GameSessionStatus,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)

from .common import (
    _AccountOperations,
    _credit_free,
    _encode_json,
    _integer,
    _json_object,
    _load_receipt,
    _lock_account,
    _lock_receipt_key,
    _read_account,
    _save_receipt,
)

_GAMBLE_SCOPE = "global"
"""@brief 保留旧进程级单一奖池语义的持久化 scope / Durable scope preserving the legacy process-wide single pool."""


class PostgresGambleOperations(_AccountOperations, GambleOperations):
    """@brief 多人奖池短事务、稳定锁序与回执 adapter / Gamble-pool adapter with short transactions, stable lock order, and receipts."""

    def __init__(
        self,
        *,
        admin_user_id: int,
        outbox: StandaloneOutboxWriter | None = None,
    ) -> None:
        """@brief 注入管理员身份与共享 outbox primitive / Inject administrator identity and the shared outbox primitive.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        @param outbox Conversation standalone-outbox primitive / Conversation standalone-outbox primitive.
        """

        super().__init__(admin_user_id=admin_user_id)
        self._outbox = outbox or PostgresOutboxRepository()
        """@brief 同事务写入通用 transactional outbox 的 primitive / Primitive writing the shared transactional outbox."""

    async def open_gamble(self, command: OpenGamble) -> GambleResult:
        """@brief 开启唯一活动奖池 / Open the sole active pool.

        @param command 开局命令 / Open command.
        @return 开局结果 / Open result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "gamble.open", command.user_id, connection
            )
            if replay is not None:
                return _gamble_result_from_json(replay, replayed=True)
            account = await _read_account(command.user_id, connection)
            if account is None:
                result = GambleResult(GambleCode.NOT_REGISTERED)
            elif account.permission < 1:
                result = GambleResult(GambleCode.PERMISSION_DENIED)
            else:
                session_id = GameSessionId(uuid4())
                row = await db_connection.fetch_one(
                    "INSERT INTO game.game_sessions "
                    "(session_id, kind, scope_key, owner_id, chat_id, message_id, state, "
                    "status, version, expires_at, created_at, updated_at) "
                    "VALUES (CAST(%s AS UUID), 'gamble', %s, %s, %s, %s, "
                    "CAST(%s AS JSONB), 'active', 0, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING session_id",
                    (
                        str(session_id),
                        _GAMBLE_SCOPE,
                        command.user_id,
                        command.chat_id,
                        command.message_id,
                        _encode_json({"bets": []}),
                        command.closes_at,
                        command.now,
                        command.now,
                    ),
                    connection=connection,
                )
                if row is None:
                    active = await _load_active_gamble(connection)
                    result = GambleResult(GambleCode.ALREADY_ACTIVE, active)
                else:
                    result = GambleResult(
                        GambleCode.SUCCESS,
                        GambleSession(
                            session_id,
                            command.chat_id,
                            command.message_id,
                            command.closes_at,
                        ),
                    )
            await _save_receipt(
                command.idempotency_key,
                "gamble.open",
                command.user_id,
                _gamble_result_to_json(result),
                connection,
            )
            return result

    async def place_gamble_bet(self, command: PlaceGambleBet) -> GambleResult:
        """@brief 原子扣费并追加一笔下注 / Atomically charge and append a wager.

        @param command 下注命令 / Wager command.
        @return 下注结果 / Wager result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "gamble.bet", command.user_id, connection
            )
            if replay is not None:
                return _gamble_result_from_json(replay, replayed=True)
            session = await _lock_gamble(command.session_id, connection)
            if session is None:
                result = GambleResult(GambleCode.NO_ACTIVE_SESSION)
            elif session.status is not GameSessionStatus.ACTIVE:
                result = GambleResult(GambleCode.NO_ACTIVE_SESSION, session)
            elif command.now >= session.closes_at:
                result = GambleResult(GambleCode.EXPIRED, session)
            elif any(bet.user_id == command.user_id for bet in session.bets):
                result = GambleResult(GambleCode.ALREADY_JOINED, session)
            else:
                account = await _lock_account(command.user_id, connection)
                if account is None:
                    result = GambleResult(GambleCode.NOT_REGISTERED, session)
                elif not await self._spend_account(account, command.amount, connection):
                    result = GambleResult(
                        GambleCode.INSUFFICIENT_COINS, session, account.total
                    )
                else:
                    updated = session.place(
                        GambleBet(
                            command.user_id, command.display_name, command.amount
                        ),
                        now=command.now,
                    )
                    affected = await db_connection.execute(
                        "UPDATE game.game_sessions SET state = CAST(%s AS JSONB), "
                        "version = %s, updated_at = %s WHERE session_id = CAST(%s AS UUID) "
                        "AND status = 'active' AND version = %s",
                        (
                            _encode_json(_gamble_state(updated)),
                            updated.version,
                            command.now,
                            str(updated.session_id),
                            session.version,
                        ),
                        connection=connection,
                    )
                    if affected != 1:
                        raise RuntimeError("Gamble OCC update lost its locked row")
                    result = GambleResult(
                        GambleCode.SUCCESS,
                        updated,
                        account.total - command.amount,
                    )
            await _save_receipt(
                command.idempotency_key,
                "gamble.bet",
                command.user_id,
                _gamble_result_to_json(result),
                connection,
            )
            return result

    async def active_gamble(self, now: datetime) -> GambleResult:
        """@brief 读取未到期活动奖池 / Read the unexpired active pool.

        @param now 当前时间 / Current time.
        @return 奖池结果 / Pool result.
        """

        session = await _load_active_gamble(None)
        if session is None:
            return GambleResult(GambleCode.NO_ACTIVE_SESSION)
        if now >= session.closes_at:
            return GambleResult(GambleCode.EXPIRED, session)
        return GambleResult(GambleCode.SUCCESS, session)

    async def due_gamble_ids(
        self, now: datetime, *, limit: int
    ) -> tuple[GameSessionId, ...]:
        """@brief 读取到期奖池 / Read due pools.

        @param now 当前时间 / Current time.
        @param limit 批量上限 / Batch bound.
        @return 会话身份 / Session identities.
        """

        rows = await db_connection.fetch_all(
            "SELECT session_id FROM game.game_sessions WHERE kind = 'gamble' "
            "AND status = 'active' AND expires_at <= %s ORDER BY expires_at, session_id "
            "LIMIT %s",
            (now, limit),
        )
        return tuple(GameSessionId(UUID(str(row[0]))) for row in rows)

    async def settle_gamble(self, command: SettleGamble) -> GambleSettlement | None:
        """@brief 结算一个到期奖池 / Settle one due pool.

        @param command 结算命令 / Settlement command.
        @return 已提交结算；未到期或不存在为 None / Committed settlement, or None when missing/not due.
        """

        if command.random_ticket < 0:
            raise ValueError("Gamble random ticket cannot be negative")
        async with db_connection.transaction() as connection:
            session = await _lock_gamble(command.session_id, connection)
            if session is None:
                return None
            if session.status is GameSessionStatus.SETTLED:
                return await _load_gamble_settlement(command.session_id, connection)
            if (
                session.status is not GameSessionStatus.ACTIVE
                or session.closes_at > command.settled_at
            ):
                return None
            draw_ticket = (
                command.random_ticket % session.prize if session.prize else None
            )
            winner = (
                session.winner_for_ticket(draw_ticket)
                if draw_ticket is not None
                else None
            )
            if winner is not None:
                account = await _lock_account(winner.user_id, connection)
                if account is None:
                    raise RuntimeError(
                        "A committed gamble participant lost its account"
                    )
                await _credit_free(winner.user_id, session.prize, connection)
            state = _gamble_state(session)
            state.update(
                {
                    "winner_id": winner.user_id if winner is not None else None,
                    "winner_name": winner.display_name if winner is not None else None,
                    "prize": session.prize,
                    "draw_ticket": draw_ticket,
                }
            )
            affected = await db_connection.execute(
                "UPDATE game.game_sessions SET state = CAST(%s AS JSONB), status = 'settled', "
                "version = version + 1, settled_at = %s, updated_at = %s "
                "WHERE session_id = CAST(%s AS UUID) AND status = 'active' AND version = %s",
                (
                    _encode_json(state),
                    command.settled_at,
                    command.settled_at,
                    str(command.session_id),
                    session.version,
                ),
                connection=connection,
            )
            if affected != 1:
                raise RuntimeError("Gamble settlement OCC update lost its locked row")
            settled = GambleSession(
                session.session_id,
                session.chat_id,
                session.message_id,
                session.closes_at,
                session.bets,
                GameSessionStatus.SETTLED,
                session.version + 1,
            )
            return GambleSettlement(
                settled,
                winner.user_id if winner is not None else None,
                winner.display_name if winner is not None else None,
                session.prize,
                False,
            )

    async def unnotified_gamble_settlements(
        self, *, limit: int
    ) -> tuple[GambleSettlement, ...]:
        """@brief 读取待通知结算 / Read settlements awaiting notification.

        @param limit 批量上限 / Batch bound.
        @return 结算快照 / Settlement snapshots.
        """

        rows = await db_connection.fetch_all(
            "SELECT session_id FROM game.game_sessions WHERE kind = 'gamble' "
            "AND status = 'settled' AND notification_enqueued_at IS NULL "
            "ORDER BY settled_at, session_id LIMIT %s",
            (limit,),
        )
        settlements: list[GambleSettlement] = []
        for row in rows:
            settlement = await _load_gamble_settlement(
                GameSessionId(UUID(str(row[0]))), None
            )
            if settlement is not None:
                settlements.append(settlement)
        return tuple(settlements)

    async def enqueue_gamble_notification(
        self,
        settlement: GambleSettlement,
        *,
        text: str,
        enqueued_at: datetime,
    ) -> None:
        """@brief 与通知标记同事务写通用 outbox / Write the shared outbox in the same transaction as notification marking.

        @param settlement 已提交结算 / Committed settlement.
        @param text 渲染文本 / Rendered text.
        @param enqueued_at 入队时间 / Enqueue time.
        @return None / None.
        """

        conversation_id = ConversationId(f"game:gamble:{settlement.session.session_id}")
        idempotency_key = "settlement:edit:v1"
        draft = OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                conversation_id, idempotency_key
            ),
            conversation_id=conversation_id,
            turn_id=None,
            delivery_stream_id=DeliveryStreamId(
                f"telegram:primary:chat:{settlement.session.chat_id}:thread:0"
            ),
            kind=EDIT_TELEGRAM_MESSAGE,
            payload={
                "chat_id": settlement.session.chat_id,
                "message_id": settlement.session.message_id,
                "text": text,
            },
            idempotency_key=idempotency_key,
            created_at=enqueued_at,
        )
        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "SELECT notification_enqueued_at FROM game.game_sessions "
                "WHERE session_id = CAST(%s AS UUID) FOR UPDATE",
                (str(settlement.session.session_id),),
                connection=connection,
            )
            if row is None:
                raise RuntimeError("Gamble settlement disappeared before notification")
            if row[0] is not None:
                return
            await self._outbox.enqueue_standalone_outbound_in_transaction(
                connection, draft
            )
            await db_connection.execute(
                "UPDATE game.game_sessions SET notification_enqueued_at = %s, "
                "updated_at = %s WHERE session_id = CAST(%s AS UUID)",
                (enqueued_at, enqueued_at, str(settlement.session.session_id)),
                connection=connection,
            )


def _gamble_state(session: GambleSession) -> dict[str, object]:
    """@brief 序列化奖池聚合状态 / Serialize pool aggregate state.

    @param session 奖池 / Pool.
    @return JSON 对象 / JSON object.
    """

    return {
        "bets": [
            {
                "user_id": bet.user_id,
                "display_name": bet.display_name,
                "amount": bet.amount,
            }
            for bet in session.bets
        ]
    }


def _map_gamble(row: Sequence[object]) -> GambleSession:
    """@brief 映射奖池会话行 / Map a pool-session row.

    @param row SQL 行 / SQL row.
    @return 奖池聚合 / Pool aggregate.
    """

    state = _json_object(row[4])
    raw_bets = state.get("bets", [])
    if not isinstance(raw_bets, list):
        raise ValueError("Persisted gamble bets must be an array")
    bets: list[GambleBet] = []
    for raw in raw_bets:
        if not isinstance(raw, Mapping):
            raise ValueError("Persisted gamble bet must be an object")
        bets.append(
            GambleBet(
                _integer(raw["user_id"]),
                str(raw["display_name"]),
                _integer(raw["amount"]),
            )
        )
    return GambleSession(
        GameSessionId(UUID(str(row[0]))),
        _integer(row[2]),
        _integer(row[3]),
        cast(datetime, row[7]),
        tuple(bets),
        GameSessionStatus(str(row[5])),
        _integer(row[6]),
    )


async def _load_active_gamble(
    connection: AsyncConnection | None,
) -> GambleSession | None:
    """@brief 读取活动全局奖池 / Read the active global pool.

    @param connection 可选事务 / Optional transaction.
    @return 奖池或 None / Pool or None.
    """

    row = await db_connection.fetch_one(
        "SELECT session_id, owner_id, chat_id, message_id, state, status, version, "
        "expires_at, notification_enqueued_at FROM game.game_sessions "
        "WHERE kind = 'gamble' AND scope_key = %s AND status = 'active'",
        (_GAMBLE_SCOPE,),
        connection=connection,
    )
    return _map_gamble(row) if row is not None else None


async def _lock_gamble(
    session_id: GameSessionId, connection: AsyncConnection
) -> GambleSession | None:
    """@brief 按身份锁定奖池 / Lock a pool by identity.

    @param session_id 会话 ID / Session ID.
    @param connection 活动事务 / Active transaction.
    @return 奖池或 None / Pool or None.
    """

    row = await db_connection.fetch_one(
        "SELECT session_id, owner_id, chat_id, message_id, state, status, version, "
        "expires_at, notification_enqueued_at FROM game.game_sessions "
        "WHERE session_id = CAST(%s AS UUID) AND kind = 'gamble' FOR UPDATE",
        (str(session_id),),
        connection=connection,
    )
    return _map_gamble(row) if row is not None else None


async def _load_gamble_settlement(
    session_id: GameSessionId, connection: AsyncConnection | None
) -> GambleSettlement | None:
    """@brief 读取已结算奖池 / Read a settled pool.

    @param session_id 会话 ID / Session ID.
    @param connection 可选事务 / Optional transaction.
    @return 结算或 None / Settlement or None.
    """

    row = await db_connection.fetch_one(
        "SELECT session_id, owner_id, chat_id, message_id, state, status, version, "
        "expires_at, notification_enqueued_at FROM game.game_sessions "
        "WHERE session_id = CAST(%s AS UUID) AND kind = 'gamble' AND status = 'settled'",
        (str(session_id),),
        connection=connection,
    )
    if row is None:
        return None
    session = _map_gamble(row)
    state = _json_object(row[4])
    return GambleSettlement(
        session,
        int(state["winner_id"]) if state.get("winner_id") is not None else None,
        str(state["winner_name"]) if state.get("winner_name") is not None else None,
        int(state.get("prize", session.prize)),
        row[8] is not None,
    )


def _gamble_session_to_json(session: GambleSession | None) -> object:
    """@brief 序列化奖池快照 / Serialize a pool snapshot.

    @param session 奖池或 None / Pool or None.
    @return JSON 值 / JSON value.
    """

    if session is None:
        return None
    return {
        "session_id": str(session.session_id),
        "chat_id": session.chat_id,
        "message_id": session.message_id,
        "closes_at": session.closes_at.isoformat(),
        "bets": _gamble_state(session)["bets"],
        "status": session.status.value,
        "version": session.version,
    }


def _gamble_session_from_json(value: object) -> GambleSession | None:
    """@brief 解析奖池快照 / Parse a pool snapshot.

    @param value JSON 值 / JSON value.
    @return 奖池或 None / Pool or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    raw_bets = data.get("bets", [])
    if not isinstance(raw_bets, list):
        raise ValueError("Receipt gamble bets must be an array")
    bets = tuple(
        GambleBet(
            int(_json_object(raw)["user_id"]),
            str(_json_object(raw)["display_name"]),
            int(_json_object(raw)["amount"]),
        )
        for raw in raw_bets
    )
    return GambleSession(
        GameSessionId(UUID(str(data["session_id"]))),
        int(data["chat_id"]),
        int(data["message_id"]),
        datetime.fromisoformat(str(data["closes_at"])),
        bets,
        GameSessionStatus(str(data["status"])),
        int(data["version"]),
    )


def _gamble_result_to_json(result: GambleResult) -> dict[str, object]:
    """@brief 序列化奖池结果回执 / Serialize a pool-result receipt.

    @param result 奖池结果 / Pool result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "session": _gamble_session_to_json(result.session),
        "balance": result.balance,
    }


def _gamble_result_from_json(
    value: Mapping[str, Any], *, replayed: bool
) -> GambleResult:
    """@brief 解析奖池结果回执 / Parse a pool-result receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否标记回放 / Whether to mark replay.
    @return 奖池结果 / Pool result.
    """

    return GambleResult(
        GambleCode(str(value["code"])),
        _gamble_session_from_json(value.get("session")),
        int(value["balance"]) if value.get("balance") is not None else None,
        replayed,
    )


__all__ = ["PostgresGambleOperations"]
