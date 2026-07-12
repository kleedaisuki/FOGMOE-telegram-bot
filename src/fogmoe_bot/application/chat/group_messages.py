"""@brief 群消息规范投影的类型与端口 / Types and ports for the canonical group-message projection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class GroupMessageKind(StrEnum):
    """@brief 可投影的 Telegram 群消息种类 / Projectable Telegram group-message kinds."""

    TEXT = "text"
    PHOTO = "photo"
    STICKER = "sticker"
    VOICE = "voice"
    VIDEO = "video"
    DOCUMENT = "document"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class GroupMessageObservation:
    """@brief 从 durable Update 提取的一条群消息观察 / One group-message observation extracted from a durable Update.

    @param source_update_id Telegram Update 幂等身份 / Telegram Update idempotency identity.
    @param group_id 群 chat ID / Group chat identifier.
    @param message_id 群内消息 ID / Message identifier within the group.
    @param sender_user_id 可选发送者 ID / Optional sender identifier.
    @param kind 内容种类 / Content kind.
    @param content 面向上下文的规范文本 / Canonical context text.
    @param created_at 原消息时间 / Original message time.
    @param updated_at 最近编辑或原消息时间 / Latest edit or original message time.
    @param edited 是否来自 edited_message / Whether this came from ``edited_message``.
    """

    source_update_id: int
    group_id: int
    message_id: int
    sender_user_id: int | None
    kind: GroupMessageKind
    content: str
    created_at: datetime
    updated_at: datetime
    edited: bool

    def __post_init__(self) -> None:
        """@brief 校验规范投影输入 / Validate canonical projection input."""

        if (
            isinstance(self.source_update_id, bool)
            or not isinstance(self.source_update_id, int)
            or self.source_update_id < 0
        ):
            raise ValueError("source_update_id must be non-negative")
        if (
            isinstance(self.group_id, bool)
            or not isinstance(self.group_id, int)
            or self.group_id == 0
        ):
            raise ValueError("group_id must be a non-zero integer")
        if (
            isinstance(self.message_id, bool)
            or not isinstance(self.message_id, int)
            or self.message_id <= 0
        ):
            raise ValueError("message_id must be positive")
        if self.sender_user_id is not None and (
            isinstance(self.sender_user_id, bool)
            or not isinstance(self.sender_user_id, int)
            or self.sender_user_id <= 0
        ):
            raise ValueError("sender_user_id must be positive when present")
        if not isinstance(self.kind, GroupMessageKind):
            raise TypeError("kind must be a GroupMessageKind")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if len(self.content) > 20_000:
            raise ValueError("group-message content cannot exceed 20000 characters")
        created_at = _utc(self.created_at, "created_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at cannot precede created_at")
        if not isinstance(self.edited, bool):
            raise TypeError("edited must be a bool")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class GroupMessage:
    """@brief Assistant 可读取的规范群消息 / Canonical group message readable by the Assistant.

    @param group_id 群 chat ID / Group chat identifier.
    @param message_id 群内消息 ID / Message identifier within the group.
    @param sender_user_id 可选发送者 ID / Optional sender identifier.
    @param sender_name 可选已注册名称 / Optional registered sender name.
    @param kind 内容种类 / Content kind.
    @param content 已解码文本 / Decoded content.
    @param created_at 原消息时间 / Original message time.
    @param edited 是否已经编辑 / Whether the message was edited.
    """

    group_id: int
    message_id: int
    sender_user_id: int | None
    sender_name: str | None
    kind: GroupMessageKind
    content: str
    created_at: datetime
    edited: bool

    def __post_init__(self) -> None:
        """@brief 复用观察值不变量校验读取模型 / Reuse observation invariants for the read model."""

        GroupMessageObservation(
            source_update_id=0,
            group_id=self.group_id,
            message_id=self.message_id,
            sender_user_id=self.sender_user_id,
            kind=self.kind,
            content=self.content,
            created_at=self.created_at,
            updated_at=self.created_at,
            edited=self.edited,
        )
        if self.sender_name is not None and not isinstance(self.sender_name, str):
            raise TypeError("sender_name must be a string or None")


class GroupMessageProjection(Protocol):
    """@brief 群消息规范写入与上下文读取端口 / Port for canonical group-message writes and context reads."""

    async def project(self, observation: GroupMessageObservation) -> None:
        """@brief 幂等投影一条观察 / Idempotently project one observation."""

        ...

    async def fetch_before(
        self,
        group_id: int,
        *,
        before_message_id: int | None,
        limit: int,
    ) -> Sequence[GroupMessage]:
        """@brief 读取指定消息之前的有界上下文 / Read bounded context before a message."""

        ...


def _utc(value: datetime, field: str) -> datetime:
    """@brief 规范化 aware UTC 时间 / Normalize an aware timestamp to UTC."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
