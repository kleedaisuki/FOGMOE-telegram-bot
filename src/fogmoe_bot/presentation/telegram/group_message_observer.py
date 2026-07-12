"""@brief durable Telegram 群消息投影 observer / Durable Telegram group-message projection observer."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from fogmoe_bot.application.chat.group_messages import (
    GroupMessageKind,
    GroupMessageObservation,
    GroupMessageProjection,
)
from fogmoe_bot.application.conversation.router import Observer, RoutedOperation
from fogmoe_bot.application.runtime import AggregateKey, WorkPriority
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate


class GroupMessageIngressObserver:
    """@brief 将 durable Update 投影到规范群消息表 / Project durable Updates into canonical group messages.

    @param projection 唯一数据库投影端口 / Sole database-projection port.
    @note observer 的延迟操作只执行数据库 I/O，不调用 Telegram。/
    The observer's lazy operation performs database I/O only and never calls Telegram.
    """

    def __init__(self, projection: GroupMessageProjection) -> None:
        self._projection = projection

    @property
    def name(self) -> str:
        return "group-message-projection"

    async def operation(
        self,
        update: InboundUpdate,
        *,
        primary_route: str | None,
    ) -> RoutedOperation | None:
        """@brief 构造一条 DB-only 幂等投影操作 / Build one DB-only idempotent projection operation."""

        del primary_route
        observation = extract_group_message_observation(update)
        if observation is None:
            return None

        async def call() -> None:
            """@brief 写入规范投影 / Write the canonical projection."""

            await self._projection.project(observation)

        return RoutedOperation(
            name=f"group-message.project:{observation.group_id}:{observation.message_id}",
            key=AggregateKey.of("telegram-group", observation.group_id),
            call=call,
            priority=WorkPriority.LOW,
        )


class TelegramObserverPipeline:
    """@brief 将多个纯 observer 合并为组合根的单一接点 / Combine pure observers behind the composition root's single hook.

    @param observers 按提交顺序执行的 observers / Observers executed in commit order.
    """

    def __init__(self, observers: Sequence[Observer]) -> None:
        """@brief 保存非空且名称唯一的 observer 序列 / Store a non-empty sequence with unique names."""

        values = tuple(observers)
        if not values:
            raise ValueError("Telegram observer pipeline cannot be empty")
        names = tuple(observer.name for observer in values)
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("Telegram observer names must be non-empty and unique")
        self._observers = values

    @property
    def name(self) -> str:
        return "telegram-group-observers"

    async def operation(
        self,
        update: InboundUpdate,
        *,
        primary_route: str | None,
    ) -> RoutedOperation | None:
        """@brief 收集子操作并按声明顺序组成一个 mailbox 操作 / Compose child operations in declaration order into one mailbox operation."""

        operations: list[RoutedOperation] = []
        for observer in self._observers:
            operation = await observer.operation(
                update,
                primary_route=primary_route,
            )
            if operation is not None:
                operations.append(operation)
        if not operations:
            return None
        identities = {operation.key.identity for operation in operations}
        if len(identities) != 1:
            raise RuntimeError("Telegram group observers resolved different identities")

        async def call() -> None:
            """@brief 先提交 DB 投影，再执行其他观察副作用 / Commit the DB projection before other observation effects."""

            for operation in operations:
                await operation.call()

        return RoutedOperation(
            name=f"telegram-group-observers:{update.update_id.value}",
            key=AggregateKey.of("telegram-group", *operations[0].key.identity),
            call=call,
            priority=min(operation.priority for operation in operations),
        )


def extract_group_message_observation(
    update: InboundUpdate,
) -> GroupMessageObservation | None:
    """@brief 从规范 JSON 安全抽取群消息 / Safely extract a group message from canonical JSON.

    @param update 已持久化 Telegram Update / Persisted Telegram Update.
    @return 已校验观察；非群消息或畸形载荷为 None / Validated observation, or None for non-group/malformed payloads.
    """

    payload_update_id = _integer(update.payload.get("update_id"), minimum=0)
    if payload_update_id != update.update_id.value:
        return None
    message_value = update.payload.get("message")
    edited_value = update.payload.get("edited_message")
    if (message_value is None) == (edited_value is None):
        return None
    edited = edited_value is not None
    message = _object(edited_value if edited else message_value)
    if message is None:
        return None
    chat = _object(message.get("chat"))
    if chat is None or chat.get("type") not in {"group", "supergroup"}:
        return None
    group_id = _integer(chat.get("id"))
    message_id = _integer(message.get("message_id"), minimum=1)
    created_seconds = _integer(message.get("date"), minimum=0)
    if (
        group_id is None
        or group_id == 0
        or message_id is None
        or created_seconds is None
    ):
        return None
    created_at = datetime.fromtimestamp(created_seconds, tz=UTC)
    edit_seconds = _integer(message.get("edit_date"), minimum=0)
    if edit_seconds is not None and edit_seconds < created_seconds:
        return None
    updated_at = (
        created_at
        if edit_seconds is None
        else datetime.fromtimestamp(edit_seconds, tz=UTC)
    )
    sender = _object(message.get("from"))
    sender_user_id = None if sender is None else _integer(sender.get("id"), minimum=1)
    kind, content = _content(message)
    return GroupMessageObservation(
        source_update_id=update.update_id.value,
        group_id=group_id,
        message_id=message_id,
        sender_user_id=sender_user_id,
        kind=kind,
        content=content,
        created_at=created_at,
        updated_at=updated_at,
        edited=edited,
    )


def _content(message: JsonObject) -> tuple[GroupMessageKind, str]:
    """@brief 将 Telegram 内容规范为有界可读文本 / Normalize Telegram content to bounded readable text."""

    text = message.get("text")
    if isinstance(text, str):
        return GroupMessageKind.TEXT, text
    caption = message.get("caption")
    caption_text = caption if isinstance(caption, str) else ""
    if isinstance(message.get("photo"), list):
        return GroupMessageKind.PHOTO, caption_text or "[photo]"
    sticker = _object(message.get("sticker"))
    if sticker is not None:
        emoji = sticker.get("emoji")
        return GroupMessageKind.STICKER, emoji if isinstance(
            emoji, str
        ) else "[sticker]"
    if _object(message.get("voice")) is not None:
        return GroupMessageKind.VOICE, caption_text or "[voice message]"
    if (
        _object(message.get("video")) is not None
        or _object(message.get("animation")) is not None
    ):
        return GroupMessageKind.VIDEO, caption_text or "[video message]"
    document = _object(message.get("document"))
    if document is not None:
        file_name = document.get("file_name")
        return (
            GroupMessageKind.DOCUMENT,
            caption_text or (file_name if isinstance(file_name, str) else "[document]"),
        )
    return GroupMessageKind.OTHER, caption_text or "[service message]"


def _object(value: JsonValue | None) -> JsonObject | None:
    """@brief 收窄 JSON object / Narrow a JSON object."""

    return value if isinstance(value, dict) else None


def _integer(value: JsonValue | None, *, minimum: int | None = None) -> int | None:
    """@brief 收窄严格整数 / Narrow a strict integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


__all__ = [
    "GroupMessageIngressObserver",
    "TelegramObserverPipeline",
    "extract_group_message_observation",
]
