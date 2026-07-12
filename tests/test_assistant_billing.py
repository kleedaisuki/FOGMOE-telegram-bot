"""@brief Assistant 计费预留领域状态机测试 / Assistant billing-reservation domain-state tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.economy import (
    AssistantBillingReservation,
    AssistantBillingStateError,
    AssistantBillingStatus,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""


def _reservation() -> AssistantBillingReservation:
    """@brief 构造跨 free/paid 桶的原生预留 / Build a native reservation spanning free and paid buckets.

    @return RESERVED 快照 / RESERVED snapshot.
    """

    return AssistantBillingReservation(
        turn_id=TurnId.parse("11111111-1111-4111-8111-111111111111"),
        user_id=42,
        cost=4,
        free_reserved=2,
        paid_reserved=2,
        pool_contribution=Decimal("0.80"),
        status=AssistantBillingStatus.RESERVED,
        reserved_at=NOW,
    )


def test_reservation_has_one_way_terminal_transitions() -> None:
    """@brief 预留只能一次性进入 settled 或 released / A reservation can enter settled or released only once."""

    reservation = _reservation()
    settled = reservation.settle(occurred_at=NOW + timedelta(seconds=1))
    released = reservation.release(occurred_at=NOW + timedelta(seconds=1))

    assert settled.status is AssistantBillingStatus.SETTLED
    assert settled.settle(occurred_at=NOW + timedelta(seconds=2)) is settled
    assert settled.release(occurred_at=NOW + timedelta(seconds=2)) is settled
    assert released.status is AssistantBillingStatus.RELEASED
    assert released.release(occurred_at=NOW + timedelta(seconds=2)) is released
    with pytest.raises(AssistantBillingStateError, match="cannot be settled"):
        released.settle(occurred_at=NOW + timedelta(seconds=2))


@pytest.mark.parametrize(
    ("free_reserved", "paid_reserved"),
    ((None, 4), (4, None), (1, 2), (-1, 5)),
)
def test_native_reservation_requires_an_exact_non_negative_split(
    free_reserved: int | None,
    paid_reserved: int | None,
) -> None:
    """@brief 原生 reservation 不猜测扣费桶 / Native reservations never guess charge buckets.

    @param free_reserved 免费桶数值 / Free-bucket value.
    @param paid_reserved 付费桶数值 / Paid-bucket value.
    """

    with pytest.raises(ValueError):
        AssistantBillingReservation(
            turn_id=TurnId.parse("11111111-1111-4111-8111-111111111111"),
            user_id=42,
            cost=4,
            free_reserved=free_reserved,
            paid_reserved=paid_reserved,
            pool_contribution=Decimal("0.80"),
            status=AssistantBillingStatus.RESERVED,
            reserved_at=NOW,
        )
