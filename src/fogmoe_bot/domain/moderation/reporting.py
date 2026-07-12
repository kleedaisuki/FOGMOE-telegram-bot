"""@brief 可持久化用户举报领域模型 / Persistable user-reporting domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ChatId, MessageId, UserId


class ReportRegistration(StrEnum):
    """@brief 举报登记结果 / Report-registration result."""

    ACCEPTED = "accepted"
    """@brief 首次举报已持久化 / First report was persisted."""

    DUPLICATE = "duplicate"
    """@brief 同一用户已举报同一消息 / The same user already reported the same message."""


@dataclass(frozen=True, slots=True)
class ReportKey:
    """@brief 举报幂等键 / Report idempotency key.

    @param chat_id 群组 ID / Group identifier.
    @param message_id 被举报消息 ID / Reported-message identifier.
    @param reporter_id 举报人 ID / Reporter identifier.
    """

    chat_id: ChatId
    message_id: MessageId
    reporter_id: UserId


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """@brief 与传输层无关的举报请求 / Transport-independent report request.

    @param key 举报幂等键 / Report idempotency key.
    @param reported_user_id 被举报人 ID / Reported-user identifier.
    @param reported_user_name 被举报人显示名 / Reported-user display name.
    @param reporter_name 举报人显示名 / Reporter display name.
    @param chat_title 群组标题 / Group title.
    @param reported_text 被举报文本 / Reported text.
    """

    key: ReportKey
    reported_user_id: UserId
    reported_user_name: str
    reporter_name: str
    chat_title: str
    reported_text: str


@dataclass(frozen=True, slots=True)
class ReportDeliveryResult:
    """@brief 举报通知投递结果 / Report-notification delivery result.

    @param administrator_count 可通知管理员数 / Number of eligible administrators.
    @param delivered_count 成功投递数 / Number of successful deliveries.
    """

    administrator_count: int
    delivered_count: int

    def __post_init__(self) -> None:
        """@brief 验证投递计数 / Validate delivery counts.

        @return None / None.
        @raises ValueError 计数无效 / For invalid counts.
        """

        if (
            self.administrator_count < 0
            or not 0 <= self.delivered_count <= self.administrator_count
        ):
            raise ValueError("Invalid report delivery counts")


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    """@brief 举报用例结果 / Reporting use-case outcome.

    @param registration 登记结果 / Registration result.
    @param delivery 首次登记后的可选投递结果 / Optional delivery result after first registration.
    """

    registration: ReportRegistration
    delivery: ReportDeliveryResult | None = None


__all__ = [
    "ReportDeliveryResult",
    "ReportKey",
    "ReportOutcome",
    "ReportRegistration",
    "ReportRequest",
]
