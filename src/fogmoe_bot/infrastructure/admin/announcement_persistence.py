"""@brief Admin 公告 PostgreSQL 行映射与边界校验 / PostgreSQL row mapping and boundary validation for Admin announcements."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fogmoe_bot.application.admin.models import RequestAnnouncement
from fogmoe_bot.domain.admin.announcement import (
    Announcement,
    AnnouncementCompletionAddress,
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
    AnnouncementIntent,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementRecipient,
    AnnouncementRecipientKind,
)

_ANNOUNCEMENT_COLUMNS = (
    "announcement_id, idempotency_key, requested_by, source_update_id, body, "
    "recipient_count, state, created_at, updated_at, completed_at"
)
"""@brief 主公告聚合的规范数据库列序 / Canonical database column order for main announcement aggregates."""

_RECIPIENT_COLUMNS = (
    "announcement_id, recipient_kind, chat_id, message_thread_id, "
    "reply_to_message_id, status, attempt_count, next_attempt_at, claim_token, "
    "lease_expires_at, outbound_message_id, last_error, created_at, updated_at, "
    "expanded_at, terminal_at"
)
"""@brief recipient 聚合的规范数据库列序 / Canonical database column order for recipient aggregates."""


def _qualified_announcement_columns(alias: str) -> str:
    """@brief 用内部 SQL alias 限定主公告列 / Qualify main-announcement columns with an internal SQL alias.

    @param alias adapter 内部固定 alias / Adapter-internal fixed alias.
    @return 规范限定列列表 / Canonical qualified column list.
    @note alias 不接受用户输入 / The alias never receives user input.
    """

    return ", ".join(
        f"{alias}.{column.strip()}" for column in _ANNOUNCEMENT_COLUMNS.split(",")
    )


def _qualified_recipient_columns(alias: str) -> str:
    """@brief 用内部 SQL alias 限定 recipient 列 / Qualify recipient columns with an internal SQL alias.

    @param alias adapter 内部固定 alias / Adapter-internal fixed alias.
    @return 规范限定列列表 / Canonical qualified column list.
    @note alias 不接受用户输入 / The alias never receives user input.
    """

    return ", ".join(
        f"{alias}.{column.strip()}" for column in _RECIPIENT_COLUMNS.split(",")
    )


def _announcement_intent(command: RequestAnnouncement) -> AnnouncementIntent:
    """@brief 将应用命令映射为领域意图 / Map an application command to a domain intent.

    @param command 已授权应用命令 / Authorized application command.
    @return 完整不可变领域意图 / Complete immutable domain intent.
    """

    return AnnouncementIntent(
        idempotency_key=command.idempotency_key,
        requested_by=command.actor_id,
        source_update_id=command.source_update_id,
        body=command.body,
        completion_address=AnnouncementCompletionAddress(
            chat_id=command.reply_chat_id,
            message_thread_id=command.reply_message_thread_id,
            reply_to_message_id=command.reply_message_id,
        ),
        requested_at=command.requested_at,
    )


def _completion_address(
    recipient: AnnouncementRecipient,
) -> AnnouncementCompletionAddress:
    """@brief 从已恢复 completion recipient 读取不可变地址 / Read the immutable address from a restored completion recipient.

    @param recipient completion recipient 聚合 / Completion-recipient aggregate.
    @return 最终报告地址 / Final-report address.
    @raise ValueError recipient 不是 completion 时抛出 / Raised when the recipient is not the completion receipt.
    """

    if (
        recipient.recipient_kind is not AnnouncementRecipientKind.COMPLETION
        or recipient.reply_to_message_id is None
    ):
        raise ValueError("Announcement aggregate requires its completion recipient")
    return AnnouncementCompletionAddress(
        chat_id=recipient.chat_id,
        message_thread_id=recipient.message_thread_id,
        reply_to_message_id=recipient.reply_to_message_id,
    )


def _restore_announcement(
    row: Sequence[object],
    *,
    completion_address: AnnouncementCompletionAddress,
) -> Announcement:
    """@brief 从主表十列与 completion 地址恢复聚合 / Restore the aggregate from ten main-table columns and its completion address.

    @param row 前十列为 admin.announcements 完整形状 / Row whose first ten columns are the complete admin.announcements shape.
    @param completion_address completion recipient 的不可变地址 / Immutable address of the completion recipient.
    @return 已验证主公告聚合 / Validated main announcement aggregate.
    """

    if len(row) < 10:
        raise ValueError("Announcement row has fewer than ten fields")
    return Announcement.restore(
        announcement_id=AnnouncementId.parse(cast(UUID | str, row[0])),
        idempotency_key=str(row[1]),
        requested_by=_integer(row[2]),
        source_update_id=_integer(row[3]),
        body=str(row[4]),
        completion_chat_id=completion_address.chat_id,
        completion_message_thread_id=completion_address.message_thread_id,
        completion_reply_to_message_id=completion_address.reply_to_message_id,
        recipient_count=_integer(row[5]),
        status=str(row[6]),
        created_at=cast(datetime, row[7]),
        updated_at=cast(datetime, row[8]),
        completed_at=cast(datetime | None, row[9]),
    )


def _restore_joined_announcement(row: Sequence[object]) -> Announcement:
    """@brief 从主表与 completion JOIN 行恢复聚合 / Restore the aggregate from a main-table/completion JOIN row.

    @param row 主表十列后附 completion 地址三列 / Main-table columns followed by three completion-address columns.
    @return 已验证主公告聚合 / Validated main announcement aggregate.
    """

    if len(row) < 13:
        raise ValueError("Joined announcement row has fewer than thirteen fields")
    return _restore_announcement(
        row,
        completion_address=AnnouncementCompletionAddress(
            chat_id=_integer(row[10]),
            message_thread_id=_optional_integer(row[11]),
            reply_to_message_id=_integer(row[12]),
        ),
    )


def _require_expected_announcement_post_state(
    row: Sequence[object],
    expected: Announcement,
    *,
    completion_address: AnnouncementCompletionAddress,
    operation: str,
) -> None:
    """@brief hydrate 主表后态并与领域决策逐字段比较 / Hydrate the main-table post-state and compare it field-for-field with the domain decision.

    @param row UPDATE RETURNING 的完整主表行 / Complete main-table row from UPDATE RETURNING.
    @param expected 领域计算后态 / Domain-calculated post-state.
    @param completion_address 不变 completion 地址 / Invariant completion address.
    @param operation 错误上下文 / Error context.
    @return None / None.
    """

    actual = _restore_announcement(row, completion_address=completion_address)
    if actual != expected:
        raise RuntimeError(
            f"Announcement {operation} post-state disagrees with domain transition"
        )


def _require_expected_post_state(
    row: Sequence[object],
    expected: AnnouncementRecipient,
    *,
    operation: str,
) -> None:
    """@brief restore SQL 后态并与领域决策逐字段比较 / Restore the SQL post-state and compare it field-for-field with the domain decision.

    @param row UPDATE RETURNING 的完整 recipient 行 / Complete recipient row from UPDATE RETURNING.
    @param expected 领域计算后态 / Domain-calculated post-state.
    @param operation 错误上下文 / Error context.
    @return None / None.
    @raise RuntimeError SQL 后态不一致时抛出 / Raised when the SQL post-state disagrees.
    """

    actual = _restore_recipient(row)
    if actual != expected:
        raise RuntimeError(
            f"Announcement {operation} post-state disagrees with domain transition"
        )


def _restore_recipient(row: Sequence[object]) -> AnnouncementRecipient:
    """@brief 从固定 SQL 列序恢复 durable recipient / Restore a durable recipient from the fixed SQL column order.

    @param row 前十六列为完整 recipient 持久化形状 / Row whose first sixteen columns are the complete recipient shape.
    @return 已验证领域聚合 / Validated domain aggregate.
    @raise ValueError 行字段不足或状态矩阵非法时抛出 / Raised for a short row or invalid state matrix.
    """

    if len(row) < 16:
        raise ValueError("Announcement recipient row has fewer than sixteen fields")
    return AnnouncementRecipient.restore(
        announcement_id=AnnouncementId.parse(cast(UUID | str, row[0])),
        recipient_kind=AnnouncementRecipientKind(str(row[1])),
        chat_id=_integer(row[2]),
        message_thread_id=_optional_integer(row[3]),
        reply_to_message_id=_optional_integer(row[4]),
        status=str(row[5]),
        attempt_count=_integer(row[6]),
        next_attempt_at=cast(datetime | None, row[7]),
        claim_token=cast(UUID | str | None, row[8]),
        lease_expires_at=cast(datetime | None, row[9]),
        outbound_message_id=cast(UUID | str | None, row[10]),
        last_error=None if row[11] is None else str(row[11]),
        created_at=cast(datetime, row[12]),
        updated_at=cast(datetime, row[13]),
        expanded_at=cast(datetime | None, row[14]),
        terminal_at=cast(datetime | None, row[15]),
    )


def _dispatch_content(row: Sequence[object]) -> AnnouncementDispatchContent:
    """@brief 从 claim JOIN 列恢复不可变出站内容 / Restore immutable dispatch content from claim JOIN columns.

    @param row recipient 十六列后附公告正文、总数、时间和终态计数 / Recipient columns followed by body, total, time, and terminal counts.
    @return 已验证出站内容 / Validated dispatch content.
    @raise ValueError 行字段不足或计数非法时抛出 / Raised for a short row or invalid counts.
    """

    if len(row) < 21:
        raise ValueError("Announcement claim row has fewer than twenty-one fields")
    return AnnouncementDispatchContent(
        body=str(row[16]),
        counts=AnnouncementDeliveryCounts(
            recipients=_integer(row[17]),
            delivered=_integer(row[19]),
            failed=_integer(row[20]),
        ),
        announcement_created_at=cast(datetime, row[18]),
    )


def _integer(value: object) -> int:
    """@brief 严格转换数据库整数 / Strictly convert a database integer.

    @param value 数据库值 / Database value.
    @return Python 整数 / Python integer.
    @raise ValueError 值不是整数 / The value is not an integer.
    """

    if isinstance(value, bool):
        raise ValueError("Boolean is not an Admin integer")
    return int(str(value))


def _optional_integer(value: object) -> int | None:
    """@brief 转换可空整数 / Convert an optional database integer.

    @param value 数据库值 / Database value.
    @return 整数或 None / Integer or None.
    """

    return None if value is None else _integer(value)


def _utc(value: datetime) -> datetime:
    """@brief 将 aware 时间规范为 UTC / Normalize an aware instant to UTC.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    @raise ValueError 输入为 naive datetime / The input is naive.
    """

    if value.tzinfo is None:
        raise ValueError("Admin persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _require_positive_limit(limit: int) -> None:
    """@brief 校验有界批量 / Validate a bounded batch limit.

    @param limit 批量上限 / Batch bound.
    @return None / None.
    @raise ValueError limit 非正 / The limit is not positive.
    """

    if isinstance(limit, bool) or limit < 1:
        raise ValueError("Admin batch limit must be positive")


__all__ = []
