"""@brief PostgreSQL 猜拳原子状态与结算适配器 / PostgreSQL atomic state-and-settlement adapter for RPS."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy.engine.row import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.games.rps_delivery import GameDelivery, MessageAddress
from fogmoe_bot.application.games.rps_operations import (
    RestoredGame,
    RestoredWaiting,
    RpsMatchCode,
    RpsMatchResult,
    RpsMutationCode,
    RpsMutationResult,
    RpsRecoveryState,
    WaitingTerminalStatus,
)
from fogmoe_bot.domain.games import (
    AccountStatus,
    ENTRY_FEE,
    GameId,
    GameSession,
    GameStatus,
    GameVersion,
    Payout,
    UserId,
    WaitingRoom,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import user_repository
from fogmoe_bot.infrastructure.database.repositories.user_repository import UserAccount
from fogmoe_bot.infrastructure.database.rps_codec import (
    decode_game_delivery,
    decode_session,
    decode_waiting,
    decode_waiting_delivery,
    encode_game_delivery,
    encode_session,
    encode_waiting,
    encode_waiting_delivery,
)


class _CreateConflict(Exception):
    """@brief 通过异常回滚部分创建 / Roll back a partially created waiting room."""


class PostgresRpsLedger:
    """@brief 以短事务提交 RPS 状态与金币 / Commit RPS state and coins in short transactions.

    @note 所有写事务采用 ``session -> accounts(sorted)`` 锁序；Telegram I/O 不属于此端口。/
    Every write transaction uses ``session -> accounts(sorted)`` lock ordering; Telegram I/O is outside this port.
    """

    def __init__(self, administrator_id: int) -> None:
        """@brief 注入管理员身份以保存套餐状态 / Inject the administrator identity for persisted plan state.

        @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
        @return None / None.
        @raise TypeError 管理员 ID 不是严格整数时抛出 /
            Raised when the administrator ID is not a strict integer.
        """

        if isinstance(administrator_id, bool) or not isinstance(administrator_id, int):
            raise TypeError("administrator_id must be an integer")
        self._administrator_id = administrator_id
        """@brief 用于套餐判定的管理员 ID / Administrator ID used for plan selection."""

    async def status(self, user_id: UserId) -> AccountStatus:
        """@brief 读取账户快照 / Read an account snapshot.

        @param user_id 玩家身份 / Player identity.
        @return 准入状态 / Admission status.
        """

        account = await user_repository.fetch_user_account(user_id.value)
        if account is None:
            return AccountStatus(registered=False, coins=0)
        return AccountStatus(registered=True, coins=account.total_coins)

    async def load_recovery_state(self, *, tombstone_limit: int) -> RpsRecoveryState:
        """@brief 恢复等待、活动对局与最近终态版本 / Restore waiting, active games, and recent terminal versions.

        @param tombstone_limit 终态版本上限 / Terminal-version bound.
        @return 恢复快照 / Recovery snapshot.
        """

        if tombstone_limit <= 0:
            raise ValueError("tombstone_limit must be positive")
        active_rows = await db_connection.fetch_all(
            "SELECT game_id, status, version, state, delivery "
            "FROM game.rps_sessions WHERE status IN ('waiting', 'choosing') "
            "ORDER BY created_at, game_id",
            mapping=True,
        )
        waiting: RestoredWaiting | None = None
        games: list[RestoredGame] = []
        for row in active_rows:
            status = _row_string(row, "status")
            if status == "waiting":
                if waiting is not None:
                    raise RuntimeError("database contains multiple RPS waiting rooms")
                room = decode_waiting(row["state"])
                _verify_identity(row, room.game_id, room.version)
                waiting = RestoredWaiting(
                    room,
                    decode_waiting_delivery(row["delivery"]),
                )
                continue
            session = decode_session(row["state"])
            _verify_identity(row, session.game_id, session.version)
            if session.status is not GameStatus.CHOOSING:
                raise RuntimeError("active RPS row does not contain a choosing session")
            games.append(RestoredGame(session, decode_game_delivery(row["delivery"])))

        terminal_rows = await db_connection.fetch_all(
            "SELECT game_id, version FROM game.rps_sessions "
            "WHERE terminal_at IS NOT NULL "
            "ORDER BY terminal_at DESC, game_id DESC LIMIT %s",
            (tombstone_limit,),
            mapping=True,
        )
        tombstones = tuple(
            (
                GameId(_row_string(row, "game_id")),
                GameVersion(_row_integer(row, "version")),
            )
            for row in reversed(terminal_rows)
        )
        return RpsRecoveryState(waiting, tuple(games), tombstones)

    async def create_waiting(self, room: WaitingRoom) -> bool:
        """@brief 原子创建等待房间与房主槽 / Atomically create a waiting room and host slot.

        @param room 等待房间 / Waiting room.
        @return 创建成功时为 True / True when created.
        """

        try:
            async with db_connection.transaction() as connection:
                inserted = await db_connection.fetch_one(
                    "INSERT INTO game.rps_sessions "
                    "(game_id, status, version, player_one_id, player_two_id, state, "
                    "delivery, expires_at, created_at, updated_at, terminal_at) "
                    "VALUES (%s, 'waiting', %s, %s, NULL, CAST(%s AS JSONB), NULL, "
                    "%s, %s, %s, NULL) ON CONFLICT DO NOTHING RETURNING game_id",
                    (
                        str(room.game_id),
                        room.version.value,
                        room.host.user_id.value,
                        encode_waiting(room),
                        room.expires_at,
                        room.created_at,
                        room.created_at,
                    ),
                    connection=connection,
                )
                if inserted is None:
                    raise _CreateConflict
                slot = await db_connection.fetch_one(
                    "INSERT INTO game.rps_player_slots (user_id, game_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING user_id",
                    (room.host.user_id.value, str(room.game_id)),
                    connection=connection,
                )
                if slot is None:
                    raise _CreateConflict
        except _CreateConflict:
            return False
        return True

    async def finish_waiting(
        self,
        room: WaitingRoom,
        status: WaitingTerminalStatus,
        *,
        finished_at: datetime,
    ) -> RpsMutationResult:
        """@brief 以 CAS 结束等待房间并释放房主槽 / Finish a waiting room with CAS and release its host slot."""

        async with db_connection.transaction() as connection:
            row = await _lock_session(room.game_id, connection)
            if _waiting_finish_is_replay(row, room, status):
                return RpsMutationResult(RpsMutationCode.APPLIED, room.version)
            conflict = _mutation_conflict(row, room.version, required_status="waiting")
            if conflict is not None:
                return conflict
            affected = await db_connection.execute(
                "UPDATE game.rps_sessions SET status = %s, updated_at = %s, "
                "terminal_at = %s WHERE game_id = %s AND status = 'waiting' "
                "AND version = %s",
                (
                    status.value,
                    finished_at,
                    finished_at,
                    str(room.game_id),
                    room.version.value,
                ),
                connection=connection,
            )
            if affected != 1:
                raise RuntimeError("locked RPS waiting CAS unexpectedly failed")
            await _delete_slots(room.game_id, connection)
            return RpsMutationResult(RpsMutationCode.APPLIED, room.version)

    async def start_game(
        self,
        room: WaitingRoom,
        session: GameSession,
        *,
        started_at: datetime,
    ) -> RpsMatchResult:
        """@brief 原子执行 waiting→choosing 与双方原桶扣费 / Atomically transition waiting-to-choosing and charge both original buckets."""

        if session.game_id != room.game_id or session.version != room.version.next():
            raise ValueError("session must be the direct successor of its waiting room")
        first = session.player_one.user_id
        second = session.player_two.user_id
        async with db_connection.transaction() as connection:
            row = await _lock_session(room.game_id, connection)
            if row is None:
                return RpsMatchResult(RpsMatchCode.NOT_FOUND)
            current = GameVersion(_row_integer(row, "version"))
            if _row_string(row, "status") == "choosing" and current == session.version:
                stored = decode_session(row["state"])
                if tuple(player.user_id for player in stored.players) == tuple(
                    player.user_id for player in session.players
                ):
                    return RpsMatchResult(RpsMatchCode.STARTED, current, stored)
            if _row_string(row, "status") != "waiting" or current != room.version:
                return RpsMatchResult(RpsMatchCode.STALE, current)

            accounts = await _lock_accounts((first, second), connection)
            first_account = accounts.get(first)
            if first_account is None or first_account.total_coins < ENTRY_FEE:
                await _invalidate_waiting(room, started_at, connection)
                return RpsMatchResult(RpsMatchCode.FIRST_UNAVAILABLE, room.version)
            second_account = accounts.get(second)
            if second_account is None or second_account.total_coins < ENTRY_FEE:
                return RpsMatchResult(RpsMatchCode.SECOND_UNAVAILABLE, room.version)

            guest_slot = await db_connection.fetch_one(
                "INSERT INTO game.rps_player_slots (user_id, game_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING user_id",
                (second.value, str(room.game_id)),
                connection=connection,
            )
            if guest_slot is None:
                return RpsMatchResult(RpsMatchCode.PLAYER_BUSY, room.version)

            await _spend_entry(
                first_account,
                connection,
                administrator_id=self._administrator_id,
            )
            await _spend_entry(
                second_account,
                connection,
                administrator_id=self._administrator_id,
            )
            affected = await db_connection.execute(
                "UPDATE game.rps_sessions SET status = 'choosing', version = %s, "
                "player_two_id = %s, state = CAST(%s AS JSONB), expires_at = %s, "
                "updated_at = %s WHERE game_id = %s AND status = 'waiting' "
                "AND version = %s",
                (
                    session.version.value,
                    second.value,
                    encode_session(session),
                    session.expires_at,
                    started_at,
                    str(session.game_id),
                    room.version.value,
                ),
                connection=connection,
            )
            if affected != 1:
                raise RuntimeError("locked RPS match CAS unexpectedly failed")
            return RpsMatchResult(RpsMatchCode.STARTED, session.version, session)

    async def commit_choice(
        self,
        previous: GameSession,
        updated: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 持久化选择并在终局同事务发奖 / Persist a choice and pay a terminal outcome in the same transaction."""

        _require_successor(previous, updated)
        if updated.status not in {GameStatus.CHOOSING, GameStatus.FINISHED}:
            raise ValueError("choice successor must be choosing or finished")
        async with db_connection.transaction() as connection:
            row = await _lock_session(previous.game_id, connection)
            if _session_transition_is_replay(row, updated):
                return RpsMutationResult(RpsMutationCode.APPLIED, updated.version)
            conflict = _mutation_conflict(
                row,
                previous.version,
                required_status="choosing",
            )
            if conflict is not None:
                return conflict
            terminal_at: datetime | None = None
            status = "choosing"
            if updated.status is GameStatus.FINISHED:
                if updated.outcome is None:
                    raise ValueError("finished RPS session requires an outcome")
                await _credit(updated.outcome.payouts, connection)
                status = "finished"
                terminal_at = committed_at
            affected = await db_connection.execute(
                "UPDATE game.rps_sessions SET status = %s, version = %s, "
                "state = CAST(%s AS JSONB), updated_at = %s, terminal_at = %s "
                "WHERE game_id = %s AND status = 'choosing' AND version = %s",
                (
                    status,
                    updated.version.value,
                    encode_session(updated),
                    committed_at,
                    terminal_at,
                    str(updated.game_id),
                    previous.version.value,
                ),
                connection=connection,
            )
            if affected != 1:
                raise RuntimeError("locked RPS choice CAS unexpectedly failed")
            if updated.status is GameStatus.FINISHED:
                await _delete_slots(updated.game_id, connection)
            return RpsMutationResult(RpsMutationCode.APPLIED, updated.version)

    async def cancel_game(
        self,
        previous: GameSession,
        cancelled: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 原子取消、原桶外免费退款并释放玩家槽 / Atomically cancel, refund as legacy free coins, and release player slots."""

        _require_successor(previous, cancelled)
        if cancelled.status is not GameStatus.CANCELLED:
            raise ValueError("cancelled successor must be cancelled")
        async with db_connection.transaction() as connection:
            row = await _lock_session(previous.game_id, connection)
            if _session_transition_is_replay(row, cancelled):
                return RpsMutationResult(RpsMutationCode.APPLIED, cancelled.version)
            conflict = _mutation_conflict(
                row,
                previous.version,
                required_status="choosing",
            )
            if conflict is not None:
                return conflict
            await _credit(cancelled.refunds, connection)
            affected = await db_connection.execute(
                "UPDATE game.rps_sessions SET status = 'cancelled', version = %s, "
                "state = CAST(%s AS JSONB), updated_at = %s, terminal_at = %s "
                "WHERE game_id = %s AND status = 'choosing' AND version = %s",
                (
                    cancelled.version.value,
                    encode_session(cancelled),
                    committed_at,
                    committed_at,
                    str(cancelled.game_id),
                    previous.version.value,
                ),
                connection=connection,
            )
            if affected != 1:
                raise RuntimeError("locked RPS cancellation CAS unexpectedly failed")
            await _delete_slots(cancelled.game_id, connection)
            return RpsMutationResult(RpsMutationCode.APPLIED, cancelled.version)

    async def bind_waiting_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        invitation: MessageAddress,
    ) -> bool:
        """@brief 持久化等待邀请地址 / Persist a waiting-invitation address."""

        affected = await db_connection.execute(
            "UPDATE game.rps_sessions SET delivery = CAST(%s AS JSONB), "
            "updated_at = CURRENT_TIMESTAMP WHERE game_id = %s "
            "AND status = 'waiting' AND version = %s",
            (
                encode_waiting_delivery(invitation),
                str(game_id),
                expected_version.value,
            ),
        )
        return affected == 1

    async def bind_game_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        delivery: GameDelivery,
    ) -> bool:
        """@brief 持久化活动对局投递地址 / Persist active-game delivery addresses."""

        affected = await db_connection.execute(
            "UPDATE game.rps_sessions SET delivery = CAST(%s AS JSONB), "
            "updated_at = CURRENT_TIMESTAMP WHERE game_id = %s "
            "AND status = 'choosing' AND version = %s",
            (
                encode_game_delivery(delivery),
                str(game_id),
                expected_version.value,
            ),
        )
        return affected == 1


async def _lock_session(
    game_id: GameId,
    connection: AsyncConnection,
) -> RowMapping | None:
    """@brief 锁定一条 RPS 会话 / Lock one RPS session row."""

    return await db_connection.fetch_one(
        "SELECT game_id, status, version, state, delivery "
        "FROM game.rps_sessions WHERE game_id = %s FOR UPDATE",
        (str(game_id),),
        mapping=True,
        connection=connection,
    )


def _mutation_conflict(
    row: RowMapping | None,
    expected_version: GameVersion,
    *,
    required_status: str,
) -> RpsMutationResult | None:
    """@brief 将锁定行收窄为 CAS 冲突或成功 / Narrow a locked row to a CAS conflict or success."""

    if row is None:
        return RpsMutationResult(RpsMutationCode.NOT_FOUND)
    current = GameVersion(_row_integer(row, "version"))
    if _row_string(row, "status") != required_status or current != expected_version:
        return RpsMutationResult(RpsMutationCode.STALE, current)
    return None


def _waiting_finish_is_replay(
    row: RowMapping | None,
    room: WaitingRoom,
    status: WaitingTerminalStatus,
) -> bool:
    """@brief 识别已提交的等待终结重放 / Recognize a replay of a committed waiting-room finish."""

    return (
        row is not None
        and _row_string(row, "status") == status.value
        and _row_integer(row, "version") == room.version.value
        and decode_waiting(row["state"]) == room
    )


def _session_transition_is_replay(
    row: RowMapping | None,
    updated: GameSession,
) -> bool:
    """@brief 识别已提交的会话后继重放 / Recognize a replay of an already committed session successor."""

    if row is None or _row_integer(row, "version") != updated.version.value:
        return False
    expected_status = {
        GameStatus.CHOOSING: "choosing",
        GameStatus.FINISHED: "finished",
        GameStatus.CANCELLED: "cancelled",
    }[updated.status]
    return (
        _row_string(row, "status") == expected_status
        and decode_session(row["state"]) == updated
    )


async def _lock_accounts(
    user_ids: Iterable[UserId],
    connection: AsyncConnection,
) -> dict[UserId, UserAccount | None]:
    """@brief 以稳定顺序锁定账户 / Lock accounts in stable order."""

    result: dict[UserId, UserAccount | None] = {}
    for user_id in sorted(set(user_ids)):
        result[user_id] = await user_repository.fetch_user_account(
            user_id.value,
            connection=connection,
            for_update=True,
        )
    return result


async def _spend_entry(
    account: UserAccount,
    connection: AsyncConnection,
    *,
    administrator_id: int,
) -> None:
    """@brief 以免费优先规则扣一枚入场费 / Spend one entry coin with free-first semantics.

    @param account 已锁定的账户快照 / Locked account snapshot.
    @param connection 调用方事务连接 / Caller-owned transaction connection.
    @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
    @return None / None.
    """

    if account.total_coins < ENTRY_FEE:
        raise ValueError("account cannot fund the RPS entry fee")
    if account.coins >= ENTRY_FEE:
        free_coins = account.coins - ENTRY_FEE
        paid_coins = account.coins_paid
    else:
        free_coins = 0
        paid_coins = account.coins_paid - (ENTRY_FEE - account.coins)
    await user_repository.set_coin_balances_and_plan(
        account.user_id,
        free_coins,
        paid_coins,
        _resolve_user_plan(
            account.user_id,
            paid_coins,
            administrator_id=administrator_id,
        ),
        connection=connection,
    )


async def _credit(
    payouts: tuple[Payout, ...],
    connection: AsyncConnection,
) -> None:
    """@brief 锁定收款账户后按稳定顺序增加免费金币 / Lock recipients and add free coins in stable order."""

    accounts = await _lock_accounts((payout.user_id for payout in payouts), connection)
    for payout in sorted(payouts, key=lambda item: item.user_id):
        if accounts.get(payout.user_id) is None:
            raise RuntimeError(f"RPS payout account {payout.user_id.value} disappeared")
        await user_repository.add_free_coins(
            payout.user_id.value,
            payout.coins,
            connection=connection,
        )


async def _invalidate_waiting(
    room: WaitingRoom,
    invalidated_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 在匹配事务内终结失效房间 / Terminate an invalid waiting room within the match transaction."""

    affected = await db_connection.execute(
        "UPDATE game.rps_sessions SET status = 'invalidated', updated_at = %s, "
        "terminal_at = %s WHERE game_id = %s AND status = 'waiting' AND version = %s",
        (
            invalidated_at,
            invalidated_at,
            str(room.game_id),
            room.version.value,
        ),
        connection=connection,
    )
    if affected != 1:
        raise RuntimeError("locked RPS invalidation CAS unexpectedly failed")
    await _delete_slots(room.game_id, connection)


async def _delete_slots(game_id: GameId, connection: AsyncConnection) -> None:
    """@brief 删除一局的全部活动玩家槽 / Delete all active player slots for a game."""

    await db_connection.execute(
        "DELETE FROM game.rps_player_slots WHERE game_id = %s",
        (str(game_id),),
        connection=connection,
    )


def _require_successor(previous: GameSession, updated: GameSession) -> None:
    """@brief 校验直接后继版本 / Validate a direct successor version."""

    if (
        updated.game_id != previous.game_id
        or updated.version != previous.version.next()
    ):
        raise ValueError(
            "RPS transition must advance the same aggregate by one version"
        )


def _verify_identity(
    row: RowMapping,
    game_id: GameId,
    version: GameVersion,
) -> None:
    """@brief 校验关系列与 JSON 聚合身份一致 / Verify relational and JSON aggregate identity agree."""

    if _row_string(row, "game_id") != str(game_id):
        raise RuntimeError("RPS JSON game_id differs from its row")
    if _row_integer(row, "version") != version.value:
        raise RuntimeError("RPS JSON version differs from its row")


def _row_string(row: RowMapping, key: str) -> str:
    """@brief 从数据库行读取字符串 / Read a string from a database row."""

    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"RPS row {key} must be a string")
    return value


def _row_integer(row: RowMapping, key: str) -> int:
    """@brief 从数据库行读取严格整数 / Read a strict integer from a database row."""

    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"RPS row {key} must be an integer")
    return value


def _resolve_user_plan(
    user_id: int,
    paid_coins: int,
    *,
    administrator_id: int,
) -> str:
    """@brief 按持久化账户规则解析套餐 / Resolve the persisted account plan.

    @param user_id 用户 ID / User ID.
    @param paid_coins 付费金币余额 / Paid-coin balance.
    @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
    @return ``admin``、``paid`` 或 ``free`` / ``admin``, ``paid``, or ``free``.
    """

    if user_id == administrator_id:
        return "admin"
    return "paid" if paid_coins > 0 else "free"
