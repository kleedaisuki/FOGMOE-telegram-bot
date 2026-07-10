from fogmoe_bot.domain.moderation import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.reporting import (
    InMemoryReportDeduplicator,
    ReportKey,
    ReportRegistration,
)


def test_report_deduplication_is_scoped_by_chat_and_message():
    deduplicator = InMemoryReportDeduplicator(ttl_seconds=60)
    first_chat = ReportKey(ChatId(-1001), MessageId(7))
    second_chat = ReportKey(ChatId(-1002), MessageId(7))

    assert deduplicator.register(
        first_chat,
        UserId(42),
        now=1,
    ) is ReportRegistration.ACCEPTED
    assert deduplicator.register(
        first_chat,
        UserId(42),
        now=2,
    ) is ReportRegistration.DUPLICATE
    assert deduplicator.register(
        second_chat,
        UserId(42),
        now=2,
    ) is ReportRegistration.ACCEPTED


def test_report_can_be_registered_again_after_ttl():
    deduplicator = InMemoryReportDeduplicator(ttl_seconds=60)
    key = ReportKey(ChatId(-1001), MessageId(7))

    deduplicator.register(key, UserId(42), now=1)

    assert deduplicator.register(
        key,
        UserId(42),
        now=61,
    ) is ReportRegistration.ACCEPTED
