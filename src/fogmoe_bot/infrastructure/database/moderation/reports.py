"""@brief 举报窗口幂等 PostgreSQL adapter / PostgreSQL adapter for windowed report idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.moderation.ports import ReportRepository
from fogmoe_bot.domain.moderation.reporting import ReportKey, ReportRegistration
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresModerationReportRepository(ReportRepository):
    """@brief 举报窗口登记仓储 / Windowed report-registration repository."""

    async def register_report(
        self,
        key: ReportKey,
        *,
        now: datetime,
        deduplication_window: timedelta,
    ) -> ReportRegistration:
        """@brief 原子接受新窗口或拒绝窗口内重复举报 / Atomically accept a new window or reject an in-window duplicate."""

        if deduplication_window <= timedelta(0):
            raise ValueError("deduplication_window must be positive")
        timestamp = _utc(now)
        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "INSERT INTO moderation.reports "
                "(chat_id, message_id, reporter_id, created_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (chat_id, message_id, reporter_id) DO UPDATE SET "
                "created_at = EXCLUDED.created_at "
                "WHERE moderation.reports.created_at <= %s RETURNING report_id",
                (
                    int(key.chat_id),
                    int(key.message_id),
                    int(key.reporter_id),
                    timestamp,
                    timestamp - deduplication_window,
                ),
                connection=connection,
            )
        return (
            ReportRegistration.ACCEPTED
            if row is not None
            else ReportRegistration.DUPLICATE
        )


def _utc(value: datetime) -> datetime:
    """@brief 规范为 UTC，并拒绝 naive 时间 / Normalize to UTC and reject naive timestamps."""

    if value.tzinfo is None:
        raise ValueError("Moderation timestamps must be timezone-aware")
    return value.astimezone(UTC)
