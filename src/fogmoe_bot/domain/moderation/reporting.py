"""@brief 用户举报领域模型 / User-reporting domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .models import ChatId, MessageId, UserId


class ReportRegistration(Enum):
    """@brief 举报登记结果 / Report-registration result."""

    ACCEPTED = auto()
    DUPLICATE = auto()


@dataclass(frozen=True, slots=True)
class ReportKey:
    """@brief 全局唯一被举报消息键 / Globally unique reported-message key.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param message_id Telegram 消息 ID / Telegram message ID.
    """

    chat_id: ChatId
    message_id: MessageId


@dataclass(frozen=True, slots=True)
class ReportRecord:
    """@brief 举报去重记录 / Report deduplication record.

    @param created_at 单调时钟创建时间 / Monotonic creation time.
    @param reporters 已举报用户集合 / Users who already reported.
    """

    created_at: float
    reporters: frozenset[UserId]


@dataclass(frozen=True, slots=True)
class ReportDeliveryResult:
    """@brief 举报通知投递结果 / Report-notification delivery result.

    @param administrator_count 可通知管理员数 / Number of eligible administrators.
    @param delivered_count 成功投递数 / Number of successful deliveries.
    """

    administrator_count: int
    delivered_count: int


class InMemoryReportDeduplicator:
    """@brief 按群组和消息去重举报 / Deduplicate reports by chat and message.

    @param ttl_seconds 去重窗口秒数 / Deduplication-window length in seconds.
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._records: dict[ReportKey, ReportRecord] = {}

    def register(
        self,
        key: ReportKey,
        reporter_id: UserId,
        *,
        now: float,
    ) -> ReportRegistration:
        """@brief 登记一次举报 / Register one report.

        @param key 被举报消息键 / Reported-message key.
        @param reporter_id 举报用户 ID / Reporter user ID.
        @param now 当前单调时间 / Current monotonic time.
        @return 接受或重复 / Accepted or duplicate.
        """

        current = self._records.get(key)
        if current and now - current.created_at < self._ttl_seconds:
            if reporter_id in current.reporters:
                return ReportRegistration.DUPLICATE
            self._records[key] = ReportRecord(
                created_at=current.created_at,
                reporters=current.reporters | {reporter_id},
            )
        else:
            self._records[key] = ReportRecord(
                created_at=now,
                reporters=frozenset({reporter_id}),
            )
        self._remove_expired(now)
        return ReportRegistration.ACCEPTED

    def _remove_expired(self, now: float) -> None:
        """@brief 清理过期去重记录 / Remove expired deduplication records.

        @param now 当前单调时间 / Current monotonic time.
        @return None / None.
        """

        expired = [
            key
            for key, record in self._records.items()
            if now - record.created_at >= self._ttl_seconds
        ]
        for key in expired:
            self._records.pop(key, None)
