"""@brief AdminService 授权与强类型结果测试 / AdminService authorization and strongly typed result tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.admin.models import (
    AdminCode,
    AdminStats,
    AnnouncementAcceptance,
    GroupFeatureStats,
    LogTail,
    RecentUser,
    RequestAnnouncement,
)
from fogmoe_bot.application.admin.service import AdminService
from fogmoe_bot.domain.admin import AnnouncementId


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时间 / Fixed test instant."""


class RecordingStats:
    """@brief 记录统计读取 / Record statistics reads."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.limits: list[int] = []
        """@brief 收到的样本上限 / Received sample limits."""

    async def fetch(self, *, group_limit: int) -> AdminStats:
        """@brief 记录并返回快照 / Record and return a snapshot.

        @param group_limit 样本上限 / Sample limit.
        @return 固定快照 / Fixed snapshot.
        """

        self.limits.append(group_limit)
        empty = GroupFeatureStats(0, ())
        return AdminStats(
            2,
            GroupFeatureStats(1, (-100,)),
            empty,
            empty,
            empty,
            (RecentUser(42, "Klee"),),
        )


class RecordingLogs:
    """@brief 记录日志读取 / Record log reads."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.lines: list[int] = []
        """@brief 收到的行数 / Received line counts."""

    async def tail(self, *, lines: int) -> LogTail | None:
        """@brief 记录并返回日志 / Record and return logs.

        @param lines 行数 / Line count.
        @return 固定快照 / Fixed snapshot.
        """

        self.lines.append(lines)
        return LogTail(("one\n", "two\n"), False)


class RecordingAnnouncements:
    """@brief 记录公告意图 / Record announcement intents."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.commands: list[RequestAnnouncement] = []
        """@brief 已接收命令 / Accepted commands."""

    async def accept(self, command: RequestAnnouncement) -> AnnouncementAcceptance:
        """@brief 记录公告 / Record an announcement.

        @param command 公告命令 / Announcement command.
        @return 固定接收回执 / Fixed acceptance receipt.
        """

        self.commands.append(command)
        return AnnouncementAcceptance(
            AnnouncementId.for_idempotency_key(command.idempotency_key),
            3,
            True,
        )


def _command(actor_id: int, *, body: str = " hello ") -> RequestAnnouncement:
    """@brief 构造公告命令 / Build an announcement command.

    @param actor_id 操作者 ID / Actor ID.
    @param body 公告本文 / Announcement body.
    @return 命令 / Command.
    """

    return RequestAnnouncement(
        actor_id=actor_id,
        source_update_id=10,
        idempotency_key="telegram:admin-announcement:10",
        body=body,
        reply_chat_id=actor_id,
        reply_message_id=5,
        reply_message_thread_id=None,
        requested_at=NOW,
    )


def _service(
    stats: RecordingStats,
    logs: RecordingLogs,
    announcements: RecordingAnnouncements,
) -> AdminService:
    """@brief 构造测试服务 / Build the test service.

    @param stats 统计端口 / Statistics port.
    @param logs 日志端口 / Log port.
    @param announcements 公告端口 / Announcement port.
    @return AdminService / AdminService.
    """

    return AdminService(
        administrator_id=42,
        stats=stats,
        logs=logs,
        announcements=announcements,  # type: ignore[arg-type]
    )


def test_permission_is_decided_before_any_port_call() -> None:
    """@brief 未授权请求不访问任何外部端口 / Unauthorized requests reach no external port."""

    stats = RecordingStats()
    logs = RecordingLogs()
    announcements = RecordingAnnouncements()
    service = _service(stats, logs, announcements)

    async def scenario() -> tuple[AdminCode, AdminCode, AdminCode]:
        """@brief 执行三类未授权请求 / Execute all three unauthorized request types.

        @return 三个业务代码 / Three business codes.
        """

        stats_result = await service.statistics(actor_id=7)
        logs_result = await service.log_tail(actor_id=7)
        announcement_result = await service.request_announcement(_command(7))
        return stats_result.code, logs_result.code, announcement_result.code

    assert asyncio.run(scenario()) == (
        AdminCode.PERMISSION_DENIED,
        AdminCode.PERMISSION_DENIED,
        AdminCode.PERMISSION_DENIED,
    )
    assert stats.limits == [] and logs.lines == [] and announcements.commands == []


def test_authorized_queries_return_typed_snapshots_and_enforce_bounds() -> None:
    """@brief 授权查询返回 typed projection 且边界在 service 内 / Authorized queries return typed projections with service-owned bounds."""

    stats = RecordingStats()
    logs = RecordingLogs()
    announcements = RecordingAnnouncements()
    service = _service(stats, logs, announcements)

    async def scenario() -> None:
        """@brief 执行有效与无效查询 / Execute valid and invalid queries.

        @return None / None.
        """

        stats_result = await service.statistics(actor_id=42, group_limit=12)
        logs_result = await service.log_tail(actor_id=42, lines=80)
        invalid = await service.log_tail(actor_id=42, lines=201)
        assert stats_result.code is AdminCode.SUCCESS
        assert stats_result.stats is not None
        assert stats_result.stats.recent_users[0].name == "Klee"
        assert logs_result.code is AdminCode.SUCCESS
        assert logs_result.tail == LogTail(("one\n", "two\n"), False)
        assert invalid.code is AdminCode.INVALID_REQUEST

    asyncio.run(scenario())
    assert stats.limits == [12] and logs.lines == [80]


def test_announcement_normalizes_body_before_durable_acceptance() -> None:
    """@brief 公告本文在持久化前规范化 / Announcement body is normalized before durable acceptance."""

    stats = RecordingStats()
    logs = RecordingLogs()
    announcements = RecordingAnnouncements()
    service = _service(stats, logs, announcements)

    result = asyncio.run(service.request_announcement(_command(42)))
    invalid = asyncio.run(service.request_announcement(_command(42, body="   ")))

    assert result.code is AdminCode.ACCEPTED and result.recipient_count == 3
    assert announcements.commands[0].body == "hello"
    assert invalid.code is AdminCode.INVALID_REQUEST
    assert len(announcements.commands) == 1
