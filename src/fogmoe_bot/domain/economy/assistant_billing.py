"""@brief Assistant 计费预留领域模型 / Assistant billing-reservation domain model."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.conversation.temporal import ensure_utc


class AssistantBillingStatus(StrEnum):
    """@brief Assistant 计费预留的穷尽状态 / Exhaustive Assistant billing-reservation states."""

    RESERVED = "reserved"
    """@brief 金币已冻结，尚未产生可计费结果 / Coins are reserved without a billable result yet."""

    SETTLED = "settled"
    """@brief 推理结果已提交并完成结算 / The inference result committed and billing was settled."""

    RELEASED = "released"
    """@brief 推理未产出结果，原预留已原桶退回 / No result was produced and the reservation was refunded to its original buckets."""


class AssistantBillingStateError(RuntimeError):
    """@brief 非法或互相矛盾的计费状态转移 / Illegal or contradictory billing-state transition."""


@dataclass(frozen=True, slots=True)
class AssistantBillingReservation:
    """@brief 一个 Turn 的不可变计费预留快照 / Immutable billing-reservation snapshot for one Turn.

    @param turn_id 所属 Conversation Turn / Owning Conversation Turn.
    @param user_id 被计费用户 / Billed user.
    @param cost 总费用 / Total charge.
    @param free_reserved 从免费桶预留的数量；旧 eager-charge 行未知时为 None / Amount reserved from the free bucket, or None for legacy eager-charge rows whose split is unknowable.
    @param paid_reserved 从付费桶预留的数量；旧 eager-charge 行未知时为 None / Amount reserved from the paid bucket, or None for legacy eager-charge rows whose split is unknowable.
    @param pool_contribution 成功结算后进入质押池的金额 / Amount credited to the staking pool after successful settlement.
    @param status 当前状态 / Current status.
    @param reserved_at 预留发生时刻 / Reservation time.
    @param settled_at 可选结算时刻 / Optional settlement time.
    @param released_at 可选释放时刻 / Optional release time.
    @param legacy_eager 是否由旧“立即扣费并入池”模型导入 / Whether the row was imported from the legacy eager-charge-and-pool model.
    """

    turn_id: TurnId
    user_id: int
    cost: int
    free_reserved: int | None
    paid_reserved: int | None
    pool_contribution: Decimal
    status: AssistantBillingStatus
    reserved_at: datetime
    settled_at: datetime | None = None
    released_at: datetime | None = None
    legacy_eager: bool = False

    def __post_init__(self) -> None:
        """@brief 校验金额、来源、状态与时间形状 / Validate amounts, provenance, state, and time shape.

        @return None / None.
        @raise ValueError 快照违反计费不变量时抛出 / Raised when the snapshot violates billing invariants.
        """

        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Assistant billing user_id must be positive")
        if isinstance(self.cost, bool) or self.cost <= 0:
            raise ValueError("Assistant billing cost must be positive")
        contribution = Decimal(str(self.pool_contribution))
        if contribution <= 0:
            raise ValueError("Assistant pool contribution must be positive")
        if self.legacy_eager:
            if self.free_reserved is not None or self.paid_reserved is not None:
                raise ValueError("Legacy eager billing cannot invent a bucket split")
            if self.status is not AssistantBillingStatus.SETTLED:
                raise ValueError("Legacy eager billing must be imported as settled")
        else:
            if self.free_reserved is None or self.paid_reserved is None:
                raise ValueError("Native reservations require an exact bucket split")
            if min(self.free_reserved, self.paid_reserved) < 0:
                raise ValueError("Reserved bucket amounts cannot be negative")
            if self.free_reserved + self.paid_reserved != self.cost:
                raise ValueError("Reserved bucket amounts must sum to cost")

        reserved_at = ensure_utc(self.reserved_at)
        settled_at = ensure_utc(self.settled_at) if self.settled_at else None
        released_at = ensure_utc(self.released_at) if self.released_at else None
        if self.status is AssistantBillingStatus.RESERVED:
            if settled_at is not None or released_at is not None:
                raise ValueError("Reserved billing cannot have a terminal timestamp")
        elif self.status is AssistantBillingStatus.SETTLED:
            if settled_at is None or released_at is not None:
                raise ValueError("Settled billing requires only settled_at")
            if settled_at < reserved_at:
                raise ValueError("settled_at cannot precede reserved_at")
        elif self.status is AssistantBillingStatus.RELEASED:
            if released_at is None or settled_at is not None:
                raise ValueError("Released billing requires only released_at")
            if released_at < reserved_at:
                raise ValueError("released_at cannot precede reserved_at")
        else:  # pragma: no cover - StrEnum construction already fences this branch.
            raise ValueError(f"Unsupported Assistant billing status {self.status!r}")
        object.__setattr__(self, "pool_contribution", contribution)
        object.__setattr__(self, "reserved_at", reserved_at)
        object.__setattr__(self, "settled_at", settled_at)
        object.__setattr__(self, "released_at", released_at)

    def settle(self, *, occurred_at: datetime) -> AssistantBillingReservation:
        """@brief 将预留纯函数式推进到结算 / Purely transition a reservation to settled.

        @param occurred_at 结算时刻 / Settlement time.
        @return 结算后的新快照；重复结算返回自身 / New settled snapshot, or self for an idempotent replay.
        @raise AssistantBillingStateError 已释放预留不可结算 / A released reservation cannot be settled.
        """

        timestamp = ensure_utc(occurred_at)
        if self.status is AssistantBillingStatus.SETTLED:
            return self
        if self.status is AssistantBillingStatus.RELEASED:
            raise AssistantBillingStateError(
                f"Released reservation for Turn {self.turn_id} cannot be settled"
            )
        return replace(
            self,
            status=AssistantBillingStatus.SETTLED,
            settled_at=timestamp,
        )

    def release(self, *, occurred_at: datetime) -> AssistantBillingReservation:
        """@brief 将预留纯函数式推进到释放 / Purely transition a reservation to released.

        @param occurred_at 释放时刻 / Release time.
        @return 释放后的新快照；重复释放或已结算返回自身 / New released snapshot, or self when already terminal.
        @note 已结算代表已有可计费结果；后续投递取消或失败不得退款 / Settled means a billable result exists, so later delivery cancellation or failure is not refundable.
        """

        timestamp = ensure_utc(occurred_at)
        if self.status is not AssistantBillingStatus.RESERVED:
            return self
        return replace(
            self,
            status=AssistantBillingStatus.RELEASED,
            released_at=timestamp,
        )


__all__ = [
    "AssistantBillingReservation",
    "AssistantBillingStateError",
    "AssistantBillingStatus",
]
