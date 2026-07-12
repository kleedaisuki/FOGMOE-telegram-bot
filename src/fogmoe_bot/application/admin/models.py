"""@brief Admin 用例命令、投影与结果 / Admin use-case commands, projections, and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from fogmoe_bot.domain.admin import AnnouncementId


ADMIN_SERVICE_DATA_KEY = "admin.service"
"""@brief 组合根中 AdminService 的稳定键 / Stable composition-root key for AdminService."""

ADMIN_RUNTIME_DATA_KEY = "admin.runtime"
"""@brief 组合根中 AdminRuntime 的稳定键 / Stable composition-root key for AdminRuntime."""


class AdminCode(StrEnum):
    """@brief Admin 用例的穷尽业务结果 / Exhaustive business codes for Admin use cases."""

    SUCCESS = "success"
    """@brief 查询成功 / Query succeeded."""

    ACCEPTED = "accepted"
    """@brief 新公告已接收 / A new announcement was accepted."""

    REPLAYED = "replayed"
    """@brief 同一公告意图的幂等重放 / Idempotent replay of the same announcement intent."""

    PERMISSION_DENIED = "permission_denied"
    """@brief 操作者不是配置的管理员 / Actor is not the configured administrator."""

    INVALID_REQUEST = "invalid_request"
    """@brief 参数不符合用例边界 / Arguments violate use-case bounds."""

    NOT_FOUND = "not_found"
    """@brief 可选诊断源不存在 / Optional diagnostic source does not exist."""


@dataclass(frozen=True, slots=True)
class RecentUser:
    """@brief 最近用户统计投影 / Recent-user statistics projection.

    @param user_id 用户 ID / User ID.
    @param name 存储的显示名 / Stored display name.
    """

    user_id: int
    name: str


@dataclass(frozen=True, slots=True)
class GroupFeatureStats:
    """@brief 某类群组功能的统计投影 / Statistics projection for one group feature.

    @param count 完整去重群组数 / Complete distinct-group count.
    @param group_ids 有界有序样本 / Bounded ordered sample of group IDs.
    """

    count: int
    group_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """@brief 校验投影计数 / Validate projection counts.

        @return None / None.
        @raise ValueError 计数非法 / Counts are invalid.
        """

        if self.count < 0 or self.count < len(self.group_ids):
            raise ValueError("Group projection count cannot be smaller than its sample")


@dataclass(frozen=True, slots=True)
class AdminStats:
    """@brief 强类型管理统计快照 / Strongly typed administrative statistics snapshot.

    @param user_count 用户数 / User count.
    @param keywords 关键词群组投影 / Keyword-enabled groups.
    @param verification 验证群组投影 / Verification-enabled groups.
    @param spam_control 启用垃圾控制群组投影 / Spam-control-enabled groups.
    @param charts 图表群组投影 / Chart-configured groups.
    @param recent_users 最近用户 / Recent users.
    """

    user_count: int
    keywords: GroupFeatureStats
    verification: GroupFeatureStats
    spam_control: GroupFeatureStats
    charts: GroupFeatureStats
    recent_users: tuple[RecentUser, ...]

    def __post_init__(self) -> None:
        """@brief 校验用户计数 / Validate the user count.

        @return None / None.
        @raise ValueError 用户计数为负 / User count is negative.
        """

        if self.user_count < 0:
            raise ValueError("Admin user count cannot be negative")


@dataclass(frozen=True, slots=True)
class LogTail:
    """@brief 有界日志尾部快照 / Bounded log-tail snapshot.

    @param lines 时间顺序的最后若干行 / Last lines in chronological order.
    @param truncated 是否因字节边界丢弃更早内容 / Whether older content was dropped at the byte bound.
    """

    lines: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class RequestAnnouncement:
    """@brief 创建公告意图与受众快照的命令 / Command creating an announcement intent and audience snapshot.

    @param actor_id 请求管理员 ID / Requesting administrator ID.
    @param source_update_id Telegram Update ID / Telegram Update ID.
    @param idempotency_key 稳定来源幂等键 / Stable source idempotency key.
    @param body 公告本文 / Announcement body.
    @param reply_chat_id 终态报告 chat ID / Terminal-report chat ID.
    @param reply_message_id 终态报告回复消息 ID / Terminal-report replied-to message ID.
    @param reply_message_thread_id 终态报告 topic ID / Terminal-report topic ID.
    @param requested_at 来源 Update 接收时间 / Source Update receipt instant.
    """

    actor_id: int
    source_update_id: int
    idempotency_key: str
    body: str
    reply_chat_id: int
    reply_message_id: int
    reply_message_thread_id: int | None
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验公告命令 / Validate the announcement command.

        @return None / None.
        @raise ValueError 标识、地址或时间非法 / Invalid identity, address, or timestamp.
        """

        if self.source_update_id < 0:
            raise ValueError("Announcement source update ID cannot be negative")
        key = self.idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError(
                "Announcement idempotency key must contain 1-255 characters"
            )
        if self.reply_chat_id == 0 or self.reply_message_id < 1:
            raise ValueError("Announcement reply address is invalid")
        if (
            self.reply_message_thread_id is not None
            and self.reply_message_thread_id < 1
        ):
            raise ValueError("Announcement reply thread ID must be positive")
        requested_at = _utc(self.requested_at)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class AnnouncementAcceptance:
    """@brief 持久化层返回的规范公告回执 / Canonical announcement receipt returned by persistence.

    @param announcement_id 公告 ID / Announcement ID.
    @param recipient_count 快照受众数 / Snapshotted audience count.
    @param inserted 本次是否创建了意图 / Whether this call created the intent.
    """

    announcement_id: AnnouncementId
    recipient_count: int
    inserted: bool

    def __post_init__(self) -> None:
        """@brief 校验受众数 / Validate the audience count.

        @return None / None.
        @raise ValueError 计数为负 / The count is negative.
        """

        if self.recipient_count < 0:
            raise ValueError("Announcement recipient count cannot be negative")


@dataclass(frozen=True, slots=True)
class AdminStatsResult:
    """@brief 统计用例结果 / Statistics use-case result.

    @param code 业务代码 / Business code.
    @param stats 授权成功时的快照 / Snapshot when authorized and successful.
    """

    code: AdminCode
    stats: AdminStats | None = None


@dataclass(frozen=True, slots=True)
class AdminLogsResult:
    """@brief 日志用例结果 / Log-tail use-case result.

    @param code 业务代码 / Business code.
    @param tail 授权成功时的有界快照 / Bounded snapshot when authorized and successful.
    """

    code: AdminCode
    tail: LogTail | None = None


@dataclass(frozen=True, slots=True)
class AnnouncementRequestResult:
    """@brief 公告用例结果 / Announcement use-case result.

    @param code 业务代码 / Business code.
    @param announcement_id 规范公告 ID / Canonical announcement ID.
    @param recipient_count 受众快照数 / Audience-snapshot count.
    """

    code: AdminCode
    announcement_id: AnnouncementId | None = None
    recipient_count: int = 0


def _utc(value: datetime) -> datetime:
    """@brief 将 aware 时间规范为 UTC / Normalize an aware instant to UTC.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    @raise ValueError 输入为 naive datetime / The input is naive.
    """

    if value.tzinfo is None:
        raise ValueError("Admin timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ADMIN_RUNTIME_DATA_KEY",
    "ADMIN_SERVICE_DATA_KEY",
    "AdminCode",
    "AdminLogsResult",
    "AdminStats",
    "AdminStatsResult",
    "AnnouncementAcceptance",
    "AnnouncementRequestResult",
    "GroupFeatureStats",
    "LogTail",
    "RecentUser",
    "RequestAnnouncement",
]
