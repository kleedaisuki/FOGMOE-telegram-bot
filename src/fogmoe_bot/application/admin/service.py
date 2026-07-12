"""@brief Admin 授权与用例协调 / Administrative authorization and use-case coordination."""

from __future__ import annotations

from .models import (
    AdminCode,
    AdminLogsResult,
    AdminStatsResult,
    AnnouncementRequestResult,
    RequestAnnouncement,
)
from .ports import AdminAnnouncementOperations, AdminLogSource, AdminStatsProjection


class AdminService:
    """@brief 在应用边界强制管理员权限 / Enforce administrator authorization at the application boundary."""

    def __init__(
        self,
        *,
        administrator_id: int,
        stats: AdminStatsProjection,
        logs: AdminLogSource,
        announcements: AdminAnnouncementOperations,
    ) -> None:
        """@brief 注入管理员 identity 与完全端口 / Inject administrator identity and all use-case ports.

        @param administrator_id 唯一管理员 ID / Sole administrator ID.
        @param stats 统计读投影 / Statistics read projection.
        @param logs 有界日志源 / Bounded log source.
        @param announcements 公告意图与回执端口 / Announcement-intent and receipt port.
        @raise ValueError 管理员 ID 非正 / Administrator ID is not positive.
        """

        if isinstance(administrator_id, bool) or administrator_id < 1:
            raise ValueError("administrator_id must be a positive integer")
        self._administrator_id = administrator_id
        self._stats = stats
        self._logs = logs
        self._announcements = announcements

    async def statistics(
        self,
        *,
        actor_id: int,
        group_limit: int = 20,
    ) -> AdminStatsResult:
        """@brief 授权后读取统计快照 / Read a statistics snapshot after authorization.

        @param actor_id 请求者 ID / Requesting actor ID.
        @param group_limit 每类群组样本上限 / Per-feature group sample limit.
        @return 类型化结果 / Typed result.
        """

        if not self._authorized(actor_id):
            return AdminStatsResult(AdminCode.PERMISSION_DENIED)
        if group_limit < 1 or group_limit > 50:
            return AdminStatsResult(AdminCode.INVALID_REQUEST)
        snapshot = await self._stats.fetch(group_limit=group_limit)
        return AdminStatsResult(AdminCode.SUCCESS, snapshot)

    async def log_tail(
        self,
        *,
        actor_id: int,
        lines: int = 50,
    ) -> AdminLogsResult:
        """@brief 授权后读取有界日志尾部 / Read a bounded log tail after authorization.

        @param actor_id 请求者 ID / Requesting actor ID.
        @param lines 行数上限 / Maximum line count.
        @return 类型化结果 / Typed result.
        """

        if not self._authorized(actor_id):
            return AdminLogsResult(AdminCode.PERMISSION_DENIED)
        if lines < 1 or lines > 200:
            return AdminLogsResult(AdminCode.INVALID_REQUEST)
        tail = await self._logs.tail(lines=lines)
        if tail is None:
            return AdminLogsResult(AdminCode.NOT_FOUND)
        return AdminLogsResult(AdminCode.SUCCESS, tail)

    async def request_announcement(
        self,
        command: RequestAnnouncement,
    ) -> AnnouncementRequestResult:
        """@brief 授权、校验并持久化公告意图 / Authorize, validate, and persist an announcement intent.

        @param command 公告命令 / Announcement command.
        @return 接收、重放或拒绝结果 / Accepted, replayed, or rejected result.
        """

        if not self._authorized(command.actor_id):
            return AnnouncementRequestResult(AdminCode.PERMISSION_DENIED)
        body = command.body.strip()
        if not body or len(body) > 3500:
            return AnnouncementRequestResult(AdminCode.INVALID_REQUEST)
        normalized = RequestAnnouncement(
            actor_id=command.actor_id,
            source_update_id=command.source_update_id,
            idempotency_key=command.idempotency_key,
            body=body,
            reply_chat_id=command.reply_chat_id,
            reply_message_id=command.reply_message_id,
            reply_message_thread_id=command.reply_message_thread_id,
            requested_at=command.requested_at,
        )
        acceptance = await self._announcements.accept(normalized)
        return AnnouncementRequestResult(
            AdminCode.ACCEPTED if acceptance.inserted else AdminCode.REPLAYED,
            acceptance.announcement_id,
            acceptance.recipient_count,
        )

    def _authorized(self, actor_id: int) -> bool:
        """@brief 以配置的 identity 进行唯一权限判断 / Make the sole authorization decision from configured identity.

        @param actor_id 请求者 ID / Requesting actor ID.
        @return 严格匹配时为 True / True only for an exact match.
        """

        return (
            not isinstance(actor_id, bool)
            and isinstance(actor_id, int)
            and actor_id == self._administrator_id
        )


__all__ = ["AdminService"]
