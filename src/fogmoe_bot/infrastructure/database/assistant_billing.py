"""@brief PostgreSQL Assistant 计费预留事务原语 / PostgreSQL Assistant billing-reservation transaction primitives."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.economy import (
    AssistantBillingReservation,
    AssistantBillingStateError,
    AssistantBillingStatus,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


_STAKE_POOL_ID = 1
"""@brief 产品单一质押奖励池 ID / Product singleton staking-pool ID."""

_RESERVATION_COLUMNS = (
    "turn_id, user_id, cost, free_reserved, paid_reserved, pool_contribution, "
    "status, reserved_at, settled_at, released_at, legacy_eager"
)
"""@brief 计费预留规范 SELECT 列 / Canonical billing-reservation SELECT columns."""


class AssistantBillingTransactions(Protocol):
    """@brief 同 PostgreSQL 事务内组合计费所需的窄端口 / Narrow port for composing billing in one PostgreSQL transaction."""

    async def reserve(
        self,
        connection: AsyncConnection,
        reservation: AssistantBillingReservation,
    ) -> bool:
        """@brief 建立幂等预留 / Create an idempotent reservation.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param reservation 初始 RESERVED 快照 / Initial RESERVED snapshot.
        @return 新插入为 True，规范 replay 为 False / True for insertion and False for a canonical replay.
        """

        ...

    async def validate_expected(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        user_id: int,
        cost: int,
        pool_contribution: Decimal | None,
    ) -> AssistantBillingReservation | None:
        """@brief 校验 replay 的收费 identity / Validate billing identity for a replay.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param user_id 预期用户 / Expected user.
        @param cost 预期费用 / Expected cost.
        @param pool_contribution 正费用的预期池贡献 / Expected pool contribution for a positive cost.
        @return 正费用的规范预留；零费用返回 None / Canonical reservation for positive cost, or None for zero cost.
        """

        ...

    async def settle(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        settled_at: datetime,
    ) -> AssistantBillingReservation | None:
        """@brief 结算预留并写稳定奖池 posting / Settle a reservation and append a stable pool posting.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param settled_at 结算时刻 / Settlement time.
        @return 规范预留；零费用 Turn 为 None / Canonical reservation, or None for a zero-cost Turn.
        """

        ...

    async def release(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        released_at: datetime,
    ) -> AssistantBillingReservation | None:
        """@brief 原桶退款并释放预留 / Refund exact buckets and release a reservation.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param released_at 释放时刻 / Release time.
        @return 规范预留；零费用 Turn 为 None / Canonical reservation, or None for a zero-cost Turn.
        """

        ...


class PostgresAssistantBilling:
    """@brief 用行锁、CAS 与稳定 posting 实现 reserve→settle/release / Implement reserve-to-settle-or-release with row locks, CAS, and stable postings."""

    def __init__(self, administrator_id: int) -> None:
        """@brief 注入管理员身份以恢复账户套餐 / Inject the administrator identity for restoring account plans.

        @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
        @return None / None.
        @raise TypeError 管理员 ID 不是严格整数时抛出 /
            Raised when the administrator ID is not a strict integer.
        """

        if isinstance(administrator_id, bool) or not isinstance(administrator_id, int):
            raise TypeError("administrator_id must be an integer")
        self._administrator_id = administrator_id
        """@brief 用于套餐恢复的管理员 ID / Administrator ID used for plan restoration."""

    async def reserve(
        self,
        connection: AsyncConnection,
        reservation: AssistantBillingReservation,
    ) -> bool:
        """@brief 在调用方事务内首次建立预留 / Establish a reservation inside the caller transaction.

        @param connection 调用方拥有的活动事务 / Active caller-owned transaction.
        @param reservation 初始 RESERVED 快照 / Initial RESERVED snapshot.
        @return 新插入为 True，等义 replay 为 False / True when inserted, False for an equivalent replay.
        @raise ValueError 非初始原生预留 / The snapshot is not an initial native reservation.
        @raise IdempotencyConflictError 同 Turn 已代表不同收费事实 / The Turn already denotes different billing semantics.
        """

        if (
            reservation.status is not AssistantBillingStatus.RESERVED
            or reservation.legacy_eager
        ):
            raise ValueError("reserve requires an initial native RESERVED snapshot")
        row = await db_connection.fetch_one(
            "INSERT INTO assistant.billing_reservations "
            "(turn_id, user_id, cost, free_reserved, paid_reserved, "
            "pool_contribution, status, reserved_at, legacy_eager) "
            "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, 'reserved', %s, FALSE) "
            "ON CONFLICT (turn_id) DO NOTHING RETURNING " + _RESERVATION_COLUMNS,
            (
                str(reservation.turn_id),
                reservation.user_id,
                reservation.cost,
                reservation.free_reserved,
                reservation.paid_reserved,
                reservation.pool_contribution,
                reservation.reserved_at,
            ),
            connection=connection,
        )
        if row is not None:
            inserted = _map_reservation(row)
            _validate_same_reservation(inserted, reservation)
            return True
        existing = await self._load_for_update(
            connection,
            reservation.turn_id,
        )
        if existing is None:
            raise RuntimeError("Billing reservation conflicted but no row exists")
        _validate_same_reservation(existing, reservation)
        return False

    async def validate_expected(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        user_id: int,
        cost: int,
        pool_contribution: Decimal | None,
    ) -> AssistantBillingReservation | None:
        """@brief 校验 replay 未改变费用、用户或奖池金额 / Validate that a replay did not change cost, user, or pool amount.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param user_id 预期用户 / Expected user.
        @param cost 预期费用 / Expected cost.
        @param pool_contribution 正费用的预期池贡献 / Expected pool contribution for positive cost.
        @return 正费用的规范预留；零费用为 None / Canonical reservation for positive cost, or None for zero cost.
        @raise IdempotencyConflictError 费用 identity 漂移或零费用产生 ledger 行 / Billing identity drifted or a zero-cost Turn owns a ledger row.
        """

        if isinstance(cost, bool) or cost < 0:
            raise ValueError("Assistant billing replay cost cannot be negative")
        existing = await self._load_for_update(connection, turn_id)
        if cost == 0:
            if existing is not None:
                raise IdempotencyConflictError(
                    f"Zero-cost Turn {turn_id} unexpectedly owns a billing reservation"
                )
            if pool_contribution is not None:
                raise ValueError("Zero-cost billing cannot have a pool contribution")
            return None
        if pool_contribution is None:
            raise ValueError("Positive-cost billing requires a pool contribution")
        expected_contribution = Decimal(str(pool_contribution))
        if existing is None:
            raise IdempotencyConflictError(
                f"Billable Turn {turn_id} is missing its billing reservation"
            )
        if (
            existing.user_id != user_id
            or existing.cost != cost
            or existing.pool_contribution != expected_contribution
        ):
            raise IdempotencyConflictError(
                f"Billing replay for Turn {turn_id} changed semantics"
            )
        return existing

    async def settle(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        settled_at: datetime,
    ) -> AssistantBillingReservation | None:
        """@brief 原子结算并以 Turn ID 去重奖池 posting / Atomically settle and deduplicate the pool posting by Turn ID.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param settled_at 结算时刻 / Settlement time.
        @return 规范终态；零费用 Turn 为 None / Canonical terminal snapshot, or None for zero cost.
        @raise AssistantBillingStateError 已释放预留不可结算 / A released reservation cannot be settled.
        @note 旧 eager 行已在迁移前入池，因此 replay 不追加新的 posting / Legacy eager rows already funded the pool before migration and append no new posting on replay.
        """

        timestamp = ensure_utc(settled_at)
        current = await self._load_for_update(connection, turn_id)
        if current is None:
            return None
        transitioned = current.settle(occurred_at=timestamp)
        if current.status is AssistantBillingStatus.SETTLED:
            if not current.legacy_eager:
                await self._ensure_pool_posting(connection, current)
            return current
        await self._ensure_pool_posting(connection, transitioned)
        row = await db_connection.fetch_one(
            "UPDATE assistant.billing_reservations "
            "SET status = 'settled', settled_at = %s "
            "WHERE turn_id = CAST(%s AS UUID) AND status = 'reserved' RETURNING "
            + _RESERVATION_COLUMNS,
            (timestamp, str(turn_id)),
            connection=connection,
        )
        if row is None:
            raise AssistantBillingStateError(
                f"Reservation for Turn {turn_id} changed while row-locked"
            )
        settled = _map_reservation(row)
        _validate_same_reservation(settled, transitioned)
        return settled

    async def release(
        self,
        connection: AsyncConnection,
        *,
        turn_id: TurnId,
        released_at: datetime,
    ) -> AssistantBillingReservation | None:
        """@brief 对 RESERVED 做一次精确原桶退款 / Refund an exact RESERVED split once.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id 规范 Turn / Canonical Turn.
        @param released_at 释放时刻 / Release time.
        @return 规范终态；零费用 Turn 为 None / Canonical terminal snapshot, or None for zero cost.
        @note SETTLED 是幂等 no-op，确保 outbox 失败或投递阶段取消不退款 / SETTLED is an idempotent no-op so outbox failure or delivery-stage cancellation is not refunded.
        """

        timestamp = ensure_utc(released_at)
        current = await self._load_for_update(connection, turn_id)
        if current is None:
            return None
        transitioned = current.release(occurred_at=timestamp)
        if current.status is not AssistantBillingStatus.RESERVED:
            return current
        if current.free_reserved is None or current.paid_reserved is None:
            raise AssistantBillingStateError(
                f"Native reservation for Turn {turn_id} has no exact bucket split"
            )
        account_row = await db_connection.fetch_one(
            "UPDATE identity.users SET "
            "coins = coins + %s, coins_paid = coins_paid + %s, "
            "user_plan = CASE WHEN id = %s THEN 'admin' "
            "WHEN coins_paid + %s > 0 THEN 'paid' ELSE 'free' END "
            "WHERE id = %s RETURNING id",
            (
                current.free_reserved,
                current.paid_reserved,
                self._administrator_id,
                current.paid_reserved,
                current.user_id,
            ),
            connection=connection,
        )
        if account_row is None:
            raise AssistantBillingStateError(
                f"Billing account {current.user_id} disappeared before release"
            )
        row = await db_connection.fetch_one(
            "UPDATE assistant.billing_reservations "
            "SET status = 'released', released_at = %s "
            "WHERE turn_id = CAST(%s AS UUID) AND status = 'reserved' RETURNING "
            + _RESERVATION_COLUMNS,
            (timestamp, str(turn_id)),
            connection=connection,
        )
        if row is None:
            raise AssistantBillingStateError(
                f"Reservation for Turn {turn_id} changed while row-locked"
            )
        released = _map_reservation(row)
        _validate_same_reservation(released, transitioned)
        return released

    @staticmethod
    async def _load_for_update(
        connection: AsyncConnection,
        turn_id: TurnId,
    ) -> AssistantBillingReservation | None:
        """@brief 锁定一个可选预留 / Lock an optional reservation.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param turn_id Turn identity / Turn identity.
        @return 预留或 None / Reservation or None.
        """

        row = await db_connection.fetch_one(
            "SELECT " + _RESERVATION_COLUMNS + " FROM assistant.billing_reservations "
            "WHERE turn_id = CAST(%s AS UUID) FOR UPDATE",
            (str(turn_id),),
            connection=connection,
        )
        return _map_reservation(row) if row is not None else None

    @staticmethod
    async def _ensure_pool_posting(
        connection: AsyncConnection,
        reservation: AssistantBillingReservation,
    ) -> None:
        """@brief 以 Turn ID 追加或验证质押池 posting / Append or validate a staking-pool posting keyed by Turn ID.

        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @param reservation 待结算预留 / Reservation being settled.
        @return None / None.
        @raise AssistantBillingStateError 稳定 key 已被其他语义占用 / The stable key is occupied by different semantics.
        """

        key = f"assistant-billing:settle:{reservation.turn_id}"
        await db_connection.execute(
            "INSERT INTO economy.stake_reward_pool (id, balance) VALUES (%s, 0) "
            "ON CONFLICT (id) DO NOTHING",
            (_STAKE_POOL_ID,),
            connection=connection,
        )
        inserted = await db_connection.execute(
            "INSERT INTO economy.stake_pool_postings "
            "(pool_id, idempotency_key, delta) VALUES (%s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (_STAKE_POOL_ID, key, reservation.pool_contribution),
            connection=connection,
        )
        if inserted == 1:
            return
        row = await db_connection.fetch_one(
            "SELECT pool_id, delta FROM economy.stake_pool_postings "
            "WHERE idempotency_key = %s",
            (key,),
            connection=connection,
        )
        if (
            row is None
            or int(str(row[0])) != _STAKE_POOL_ID
            or Decimal(str(row[1])) != reservation.pool_contribution
        ):
            raise AssistantBillingStateError(
                f"Pool posting {key} changed billing semantics"
            )


def _map_reservation(row: object) -> AssistantBillingReservation:
    """@brief 将数据库行映射为领域快照 / Map a database row to a domain snapshot.

    @param row 数据库行 / Database row.
    @return 计费预留 / Billing reservation.
    """

    values = cast(Sequence[object], row)
    if len(values) != 11:
        raise RuntimeError(f"Expected 11 billing columns, received {len(values)}")
    reserved_at = values[7]
    settled_at = values[8]
    released_at = values[9]
    if not isinstance(reserved_at, datetime):
        raise TypeError("Billing reserved_at must be a datetime")
    if settled_at is not None and not isinstance(settled_at, datetime):
        raise TypeError("Billing settled_at must be a datetime")
    if released_at is not None and not isinstance(released_at, datetime):
        raise TypeError("Billing released_at must be a datetime")
    return AssistantBillingReservation(
        turn_id=TurnId.parse(str(values[0])),
        user_id=int(str(values[1])),
        cost=int(str(values[2])),
        free_reserved=(None if values[3] is None else int(str(values[3]))),
        paid_reserved=(None if values[4] is None else int(str(values[4]))),
        pool_contribution=Decimal(str(values[5])),
        status=AssistantBillingStatus(str(values[6])),
        reserved_at=reserved_at,
        settled_at=settled_at,
        released_at=released_at,
        legacy_eager=bool(values[10]),
    )


def _validate_same_reservation(
    actual: AssistantBillingReservation,
    expected: AssistantBillingReservation,
) -> None:
    """@brief 验证同一 Turn 的不可变计费事实 / Validate immutable billing facts for one Turn.

    @param actual 规范数据库快照 / Canonical database snapshot.
    @param expected 调用方预期快照 / Caller-expected snapshot.
    @return None / None.
    @raise IdempotencyConflictError 不可变字段漂移 / Immutable fields drifted.
    """

    immutable_actual = (
        actual.turn_id,
        actual.user_id,
        actual.cost,
        actual.free_reserved,
        actual.paid_reserved,
        actual.pool_contribution,
        actual.reserved_at,
        actual.legacy_eager,
    )
    immutable_expected = (
        expected.turn_id,
        expected.user_id,
        expected.cost,
        expected.free_reserved,
        expected.paid_reserved,
        expected.pool_contribution,
        expected.reserved_at,
        expected.legacy_eager,
    )
    if immutable_actual != immutable_expected or actual.status is not expected.status:
        raise IdempotencyConflictError(
            f"Billing reservation for Turn {expected.turn_id} changed semantics"
        )


__all__ = ["AssistantBillingTransactions", "PostgresAssistantBilling"]
