import asyncio
from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.moderation.reporting_service import ReportingService
from fogmoe_bot.domain.moderation.models import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.reporting import (
    ReportDeliveryResult,
    ReportKey,
    ReportRegistration,
    ReportRequest,
)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 11, tzinfo=UTC)


class _Repository:
    def __init__(self) -> None:
        self.keys: set[ReportKey] = set()

    async def register_report(
        self,
        key: ReportKey,
        *,
        now: datetime,
        deduplication_window: timedelta,
    ) -> ReportRegistration:
        assert now.tzinfo is not None
        assert deduplication_window == timedelta(hours=1)
        if key in self.keys:
            return ReportRegistration.DUPLICATE
        self.keys.add(key)
        return ReportRegistration.ACCEPTED


class _Delivery:
    def __init__(self) -> None:
        self.calls = 0

    async def deliver(self, request: ReportRequest) -> ReportDeliveryResult:
        del request
        self.calls += 1
        return ReportDeliveryResult(2, 2)


def _request(reporter: int = 7) -> ReportRequest:
    return ReportRequest(
        key=ReportKey(ChatId(-1001), MessageId(3), UserId(reporter)),
        reported_user_id=UserId(9),
        reported_user_name="reported",
        reporter_name="reporter",
        chat_title="group",
        reported_text="text",
    )


def test_same_reporter_and_message_are_durably_idempotent() -> None:
    repository = _Repository()
    delivery = _Delivery()
    service = ReportingService(repository, delivery, _Clock())

    first = asyncio.run(service.report(_request()))
    duplicate = asyncio.run(service.report(_request()))

    assert first.registration is ReportRegistration.ACCEPTED
    assert first.delivery == ReportDeliveryResult(2, 2)
    assert duplicate.registration is ReportRegistration.DUPLICATE
    assert duplicate.delivery is None
    assert delivery.calls == 1


def test_distinct_reporters_are_independent_idempotency_keys() -> None:
    repository = _Repository()
    delivery = _Delivery()
    service = ReportingService(repository, delivery, _Clock())

    assert (
        asyncio.run(service.report(_request(7))).registration
        is ReportRegistration.ACCEPTED
    )
    assert (
        asyncio.run(service.report(_request(8))).registration
        is ReportRegistration.ACCEPTED
    )
    assert delivery.calls == 2
