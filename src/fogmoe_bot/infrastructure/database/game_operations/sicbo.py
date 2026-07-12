"""@brief 骰宝 PostgreSQL adapter / PostgreSQL adapter for Sic Bo."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.sicbo.models import (
    CancelSicBo,
    OpenSicBo,
    PlaySicBo,
    SelectSicBoBet,
    SicBoCode,
    SicBoResult,
)
from fogmoe_bot.application.games.ports.sicbo import SicBoOperations
from fogmoe_bot.domain.games import (
    DiceRoll,
    GameSessionId,
    GameSessionStatus,
    SicBoBet,
    SicBoOutcome,
    SicBoSession,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

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


class PostgresSicBoOperations(_AccountOperations, SicBoOperations):
    """@brief 骰宝短事务、OCC 与回执 adapter / Sic Bo adapter with short transactions, OCC, and receipts."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject the administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        super().__init__(admin_user_id=admin_user_id)

    async def open_sicbo(self, command: OpenSicBo) -> SicBoResult:
        """@brief 开启一个单人骰宝会话 / Open one single-player Sic Bo session.

        @param command 开局命令 / Open command.
        @return 会话结果 / Session result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "sicbo.open", command.user_id, connection
            )
            if replay is not None:
                return _sicbo_result_from_json(replay, replayed=True)
            account = await _read_account(command.user_id, connection)
            if account is None:
                result = SicBoResult(SicBoCode.NOT_REGISTERED)
            elif account.total < 1:
                result = SicBoResult(
                    SicBoCode.INSUFFICIENT_COINS, balance=account.total
                )
            else:
                await db_connection.execute(
                    "UPDATE game.game_sessions SET status = 'expired', "
                    "version = version + 1, updated_at = %s "
                    "WHERE kind = 'sicbo' AND scope_key = %s AND status = 'active' "
                    "AND expires_at <= %s",
                    (
                        command.now,
                        f"user:{command.user_id}",
                        command.now,
                    ),
                    connection=connection,
                )
                session_id = GameSessionId(uuid4())
                row = await db_connection.fetch_one(
                    "INSERT INTO game.game_sessions "
                    "(session_id, kind, scope_key, owner_id, chat_id, message_id, state, "
                    "status, version, expires_at, created_at, updated_at) "
                    "VALUES (CAST(%s AS UUID), 'sicbo', %s, %s, %s, %s, "
                    "CAST(%s AS JSONB), 'active', 0, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING session_id",
                    (
                        str(session_id),
                        f"user:{command.user_id}",
                        command.user_id,
                        command.chat_id,
                        command.message_id,
                        _encode_json({"bet": None}),
                        command.expires_at,
                        command.now,
                        command.now,
                    ),
                    connection=connection,
                )
                if row is None:
                    active = await _load_active_sicbo(
                        command.user_id, command.now, connection
                    )
                    result = SicBoResult(SicBoCode.ALREADY_ACTIVE, active)
                else:
                    result = SicBoResult(
                        SicBoCode.SUCCESS,
                        SicBoSession(
                            session_id,
                            command.user_id,
                            command.chat_id,
                            command.message_id,
                            command.expires_at,
                        ),
                        balance=account.total,
                    )
            await _save_receipt(
                command.idempotency_key,
                "sicbo.open",
                command.user_id,
                _sicbo_result_to_json(result),
                connection,
            )
            return result

    async def active_sicbo(self, user_id: int, now: datetime) -> SicBoSession | None:
        """@brief 读取活动骰宝会话 / Read an active Sic Bo session.

        @param user_id 玩家 ID / Player ID.
        @param now 当前时间 / Current time.
        @return 会话或 None / Session or None.
        """

        return await _load_active_sicbo(user_id, now, None)

    async def select_sicbo_bet(self, command: SelectSicBoBet) -> SicBoResult:
        """@brief OCC 更新骰宝下注类型 / Update Sic Bo wager using OCC.

        @param command 选择命令 / Selection command.
        @return 会话结果 / Session result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "sicbo.select", command.user_id, connection
            )
            if replay is not None:
                return _sicbo_result_from_json(replay, replayed=True)
            session = await _lock_sicbo(command.session_id, connection)
            result: SicBoResult
            if session is None or session.owner_id != command.user_id:
                result = SicBoResult(SicBoCode.NO_ACTIVE_SESSION)
            elif command.expected_version is not None and (
                command.expected_version != session.version
            ):
                result = SicBoResult(SicBoCode.STALE_VERSION, session)
            else:
                try:
                    updated = session.choose(command.bet, now=command.now)
                except ValueError:
                    result = SicBoResult(SicBoCode.EXPIRED, session)
                else:
                    await _update_sicbo_session(
                        updated, session.version, command.now, connection
                    )
                    result = SicBoResult(SicBoCode.SUCCESS, updated)
            await _save_receipt(
                command.idempotency_key,
                "sicbo.select",
                command.user_id,
                _sicbo_result_to_json(result),
                connection,
            )
            return result

    async def cancel_sicbo(self, command: CancelSicBo) -> SicBoResult:
        """@brief OCC 取消骰宝 / Cancel Sic Bo using OCC.

        @param command 取消命令 / Cancellation command.
        @return 会话结果 / Session result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "sicbo.cancel", command.user_id, connection
            )
            if replay is not None:
                return _sicbo_result_from_json(replay, replayed=True)
            session = await _lock_sicbo(command.session_id, connection)
            if session is None or session.owner_id != command.user_id:
                result = SicBoResult(SicBoCode.NO_ACTIVE_SESSION)
            elif command.expected_version is not None and (
                command.expected_version != session.version
            ):
                result = SicBoResult(SicBoCode.STALE_VERSION, session)
            else:
                try:
                    updated = session.cancel(now=command.now)
                except ValueError:
                    result = SicBoResult(SicBoCode.EXPIRED, session)
                else:
                    await _update_sicbo_session(
                        updated, session.version, command.now, connection
                    )
                    result = SicBoResult(SicBoCode.SUCCESS, updated)
            await _save_receipt(
                command.idempotency_key,
                "sicbo.cancel",
                command.user_id,
                _sicbo_result_to_json(result),
                connection,
            )
            return result

    async def play_sicbo(self, command: PlaySicBo) -> SicBoResult:
        """@brief 原子扣费、派奖与结束骰宝 / Atomically charge, pay, and finish Sic Bo.

        @param command 结算命令 / Settlement command.
        @return 结算结果 / Settlement result.
        """

        async with db_connection.transaction() as connection:
            await _lock_receipt_key(command.idempotency_key, connection)
            replay = await _load_receipt(
                command.idempotency_key, "sicbo.play", command.user_id, connection
            )
            if replay is not None:
                return _sicbo_result_from_json(replay, replayed=True)
            session = await _lock_sicbo(command.session_id, connection)
            if session is None or session.owner_id != command.user_id:
                result = SicBoResult(SicBoCode.NO_ACTIVE_SESSION)
            elif command.expected_version is not None and (
                command.expected_version != session.version
            ):
                result = SicBoResult(SicBoCode.STALE_VERSION, session)
            elif session.status is not GameSessionStatus.ACTIVE:
                result = SicBoResult(SicBoCode.NO_ACTIVE_SESSION, session)
            elif command.now >= session.expires_at:
                result = SicBoResult(SicBoCode.EXPIRED, session)
            elif session.bet is None:
                result = SicBoResult(SicBoCode.INVALID, session)
            else:
                account = await _lock_account(command.user_id, connection)
                if account is None:
                    result = SicBoResult(SicBoCode.NOT_REGISTERED, session)
                elif not await self._spend_account(account, command.amount, connection):
                    result = SicBoResult(
                        SicBoCode.INSUFFICIENT_COINS,
                        session,
                        balance=account.total,
                    )
                else:
                    outcome = SicBoOutcome.resolve(
                        session.bet, command.amount, command.roll
                    )
                    if outcome.credited:
                        await _credit_free(
                            command.user_id, outcome.credited, connection
                        )
                    balance = account.total - command.amount + outcome.credited
                    updated = SicBoSession(
                        session.session_id,
                        session.owner_id,
                        session.chat_id,
                        session.message_id,
                        session.expires_at,
                        session.bet,
                        GameSessionStatus.SETTLED,
                        session.version + 1,
                    )
                    affected = await db_connection.execute(
                        "UPDATE game.game_sessions SET state = CAST(%s AS JSONB), "
                        "status = 'settled', version = %s, settled_at = %s, updated_at = %s "
                        "WHERE session_id = CAST(%s AS UUID) AND status = 'active' AND version = %s",
                        (
                            _encode_json(
                                {
                                    "bet": session.bet.value,
                                    "outcome": _sicbo_outcome_to_json(outcome),
                                }
                            ),
                            updated.version,
                            command.now,
                            command.now,
                            str(session.session_id),
                            session.version,
                        ),
                        connection=connection,
                    )
                    if affected != 1:
                        raise RuntimeError("Sic Bo OCC update lost its locked row")
                    result = SicBoResult(SicBoCode.SUCCESS, updated, outcome, balance)
            await _save_receipt(
                command.idempotency_key,
                "sicbo.play",
                command.user_id,
                _sicbo_result_to_json(result),
                connection,
            )
            return result

    async def expire_sicbo(self, now: datetime, *, limit: int) -> int:
        """@brief 有界过期骰宝会话 / Expire Sic Bo sessions in a bounded batch.

        @param now 当前时间 / Current time.
        @param limit 批量上限 / Batch bound.
        @return 过期数量 / Expired count.
        """

        return await db_connection.execute(
            "WITH due AS (SELECT session_id FROM game.game_sessions "
            "WHERE kind = 'sicbo' AND status = 'active' AND expires_at <= %s "
            "ORDER BY expires_at, session_id LIMIT %s FOR UPDATE SKIP LOCKED) "
            "UPDATE game.game_sessions AS sessions SET status = 'expired', "
            "version = sessions.version + 1, updated_at = %s FROM due "
            "WHERE sessions.session_id = due.session_id",
            (now, limit, now),
        )


def _map_sicbo(row: Sequence[object]) -> SicBoSession:
    """@brief 映射骰宝会话行 / Map a Sic Bo session row.

    @param row SQL 行 / SQL row.
    @return 骰宝聚合 / Sic Bo aggregate.
    """

    state = _json_object(row[4])
    raw_bet = state.get("bet")
    return SicBoSession(
        GameSessionId(UUID(str(row[0]))),
        _integer(row[1]),
        _integer(row[2]),
        _integer(row[3]),
        cast(datetime, row[7]),
        SicBoBet(str(raw_bet)) if raw_bet is not None else None,
        GameSessionStatus(str(row[5])),
        _integer(row[6]),
    )


async def _load_active_sicbo(
    user_id: int,
    now: datetime,
    connection: AsyncConnection | None,
) -> SicBoSession | None:
    """@brief 读取未过期活动骰宝 / Read an unexpired active Sic Bo session.

    @param user_id 玩家 ID / Player ID.
    @param now 当前时间 / Current time.
    @param connection 可选事务 / Optional transaction.
    @return 会话或 None / Session or None.
    """

    row = await db_connection.fetch_one(
        "SELECT session_id, owner_id, chat_id, message_id, state, status, version, "
        "expires_at, notification_enqueued_at FROM game.game_sessions "
        "WHERE kind = 'sicbo' AND scope_key = %s AND status = 'active' "
        "AND expires_at > %s",
        (f"user:{user_id}", now),
        connection=connection,
    )
    return _map_sicbo(row) if row is not None else None


async def _lock_sicbo(
    session_id: GameSessionId, connection: AsyncConnection
) -> SicBoSession | None:
    """@brief 锁定骰宝会话 / Lock a Sic Bo session.

    @param session_id 会话 ID / Session ID.
    @param connection 活动事务 / Active transaction.
    @return 会话或 None / Session or None.
    """

    row = await db_connection.fetch_one(
        "SELECT session_id, owner_id, chat_id, message_id, state, status, version, "
        "expires_at, notification_enqueued_at FROM game.game_sessions "
        "WHERE session_id = CAST(%s AS UUID) AND kind = 'sicbo' FOR UPDATE",
        (str(session_id),),
        connection=connection,
    )
    return _map_sicbo(row) if row is not None else None


async def _update_sicbo_session(
    session: SicBoSession,
    expected_version: int,
    now: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 保存骰宝 OCC 转移 / Persist a Sic Bo OCC transition.

    @param session 新聚合 / New aggregate.
    @param expected_version 旧版本 / Previous version.
    @param now 更新时间 / Update time.
    @param connection 活动事务 / Active transaction.
    @return None / None.
    """

    affected = await db_connection.execute(
        "UPDATE game.game_sessions SET state = CAST(%s AS JSONB), status = %s, "
        "version = %s, updated_at = %s WHERE session_id = CAST(%s AS UUID) "
        "AND version = %s AND status = 'active'",
        (
            _encode_json({"bet": session.bet.value if session.bet else None}),
            session.status.value,
            session.version,
            now,
            str(session.session_id),
            expected_version,
        ),
        connection=connection,
    )
    if affected != 1:
        raise RuntimeError("Sic Bo OCC update lost its locked row")


def _sicbo_session_to_json(session: SicBoSession | None) -> object:
    """@brief 序列化骰宝会话 / Serialize a Sic Bo session.

    @param session 会话或 None / Session or None.
    @return JSON 值 / JSON value.
    """

    if session is None:
        return None
    return {
        "session_id": str(session.session_id),
        "owner_id": session.owner_id,
        "chat_id": session.chat_id,
        "message_id": session.message_id,
        "expires_at": session.expires_at.isoformat(),
        "bet": session.bet.value if session.bet is not None else None,
        "status": session.status.value,
        "version": session.version,
    }


def _sicbo_session_from_json(value: object) -> SicBoSession | None:
    """@brief 解析骰宝会话 / Parse a Sic Bo session.

    @param value JSON 值 / JSON value.
    @return 会话或 None / Session or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    return SicBoSession(
        GameSessionId(UUID(str(data["session_id"]))),
        int(data["owner_id"]),
        int(data["chat_id"]),
        int(data["message_id"]),
        datetime.fromisoformat(str(data["expires_at"])),
        SicBoBet(str(data["bet"])) if data.get("bet") is not None else None,
        GameSessionStatus(str(data["status"])),
        int(data["version"]),
    )


def _sicbo_outcome_to_json(outcome: SicBoOutcome) -> dict[str, object]:
    """@brief 序列化骰宝结算 / Serialize a Sic Bo settlement.

    @param outcome 结算 / Settlement.
    @return JSON 对象 / JSON object.
    """

    return {
        "bet": outcome.bet.value,
        "amount": outcome.amount,
        "dice": list(outcome.roll.dice),
        "won": outcome.won,
        "credited": outcome.credited,
    }


def _sicbo_outcome_from_json(value: object) -> SicBoOutcome | None:
    """@brief 解析可选骰宝结算 / Parse an optional Sic Bo settlement.

    @param value JSON 值 / JSON value.
    @return 结算或 None / Settlement or None.
    """

    if value is None:
        return None
    data = _json_object(value)
    raw_dice = data["dice"]
    if not isinstance(raw_dice, list) or len(raw_dice) != 3:
        raise ValueError("Receipt Sic Bo dice must contain three faces")
    return SicBoOutcome(
        SicBoBet(str(data["bet"])),
        int(data["amount"]),
        DiceRoll((int(raw_dice[0]), int(raw_dice[1]), int(raw_dice[2]))),
        bool(data["won"]),
        int(data["credited"]),
    )


def _sicbo_result_to_json(result: SicBoResult) -> dict[str, object]:
    """@brief 序列化骰宝回执 / Serialize a Sic Bo receipt.

    @param result 骰宝结果 / Sic Bo result.
    @return 版本化 JSON / Versioned JSON.
    """

    return {
        "schema": 1,
        "code": result.code.value,
        "session": _sicbo_session_to_json(result.session),
        "outcome": (
            _sicbo_outcome_to_json(result.outcome)
            if result.outcome is not None
            else None
        ),
        "balance": result.balance,
    }


def _sicbo_result_from_json(value: Mapping[str, Any], *, replayed: bool) -> SicBoResult:
    """@brief 解析骰宝回执 / Parse a Sic Bo receipt.

    @param value 回执 JSON / Receipt JSON.
    @param replayed 是否回放 / Whether replayed.
    @return 骰宝结果 / Sic Bo result.
    """

    return SicBoResult(
        SicBoCode(str(value["code"])),
        _sicbo_session_from_json(value.get("session")),
        _sicbo_outcome_from_json(value.get("outcome")),
        int(value["balance"]) if value.get("balance") is not None else None,
        replayed,
    )


__all__ = ["PostgresSicBoOperations"]
