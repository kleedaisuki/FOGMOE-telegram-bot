"""@brief 群附件上下文边界的 CTest / CTest for the group-attachment context boundary."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.chat.group_messages import GroupMessage
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.infrastructure.assistant.tool_operations.group import (
    fetch_group_context,
)
from fogmoe_bot.presentation.telegram.group_message_observer import (
    extract_group_message_observation,
)

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 测试用稳定群消息时间 / Stable group-message timestamp for tests."""

_GROUP_ATTACHMENT_MARKER = "<group_attachment />"
"""@brief 未导入群附件唯一允许的模型文本 / Sole model text allowed for an unimported group attachment."""


class _Groups:
    """@brief 将 observer 投影直接交给群上下文工具的只读替身 / Read-only double feeding observer projections directly into the group-context tool.

    @param messages 已由 observer 规范化的群消息 / Group messages normalized by the observer.
    """

    def __init__(self, messages: Sequence[GroupMessage]) -> None:
        """@brief 保存时间正序的规范消息 / Store canonical messages in chronological order.

        @param messages observer 输出的规范消息 / Canonical messages produced by the observer.
        @return None / None.
        """

        self._messages = tuple(messages)

    async def fetch_before(
        self,
        group_id: int,
        *,
        message_thread_id: int | None,
        before_message_id: int | None,
        limit: int,
    ) -> Sequence[GroupMessage]:
        """@brief 返回受请求边界约束的规范消息 / Return canonical messages bounded by the request.

        @param group_id 当前群 ID / Current group identifier.
        @param message_thread_id 当前 Topic；本测试没有 Topic / Current topic; this test has no topic.
        @param before_message_id 当前消息前的边界 / Boundary before the current message.
        @param limit 最大条数 / Maximum number of messages.
        @return 当前消息之前的 observer 投影 / Observer projections before the current message.
        """

        return tuple(
            message
            for message in self._messages
            if message.group_id == group_id
            and message.message_thread_id == message_thread_id
            and (before_message_id is None or message.message_id < before_message_id)
        )[-limit:]


def _update(
    *,
    update_id: int,
    message_id: int,
    media: dict[str, object],
) -> InboundUpdate:
    """@brief 构造带恶意展示元数据的 durable 群媒体 Update / Build a durable group-media Update with hostile display metadata.

    @param update_id Telegram Update ID / Telegram Update identifier.
    @param message_id 群内消息 ID / In-group message identifier.
    @param media 一个 Telegram 媒体字段及其用户提供元数据 / One Telegram media field and its user-provided metadata.
    @return observer 可处理的持久化 Update / Persisted Update processable by the observer.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-group:-1001:thread:0"),
        payload={
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": int(_NOW.timestamp()),
                "chat": {"id": -1001, "type": "supergroup"},
                "from": {"id": 42, "is_bot": False, "first_name": "Klee"},
                **media,
            },
        },
        received_at=_NOW,
    )


def _as_group_message(update: InboundUpdate) -> GroupMessage:
    """@brief 将 observer 观察值转换为数据库读取模型 / Convert an observer observation into the database read model.

    @param update 待投影的 durable Telegram Update / Durable Telegram Update to project.
    @return 群上下文工具可读取的规范消息 / Canonical message readable by the group-context tool.
    """

    observation = extract_group_message_observation(update)
    if observation is None:
        raise AssertionError("test media update must yield a group observation")
    return GroupMessage(
        group_id=observation.group_id,
        message_id=observation.message_id,
        sender_user_id=observation.sender_user_id,
        sender_name=observation.sender_name,
        sender_username=observation.sender_username,
        kind=observation.kind,
        content=observation.content,
        created_at=observation.created_at,
        edited=observation.edited,
        message_thread_id=observation.message_thread_id,
    )


def _request() -> ToolEffectRequest:
    """@brief 构造一个群 ``fetch_group_context`` 工具请求 / Build one group ``fetch_group_context`` tool request.

    @return 已认证、只读的工具请求 / Authenticated read-only tool request.
    """

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-group:-1001:thread:0"),
            delivery_stream_id=DeliveryStreamId("telegram:test:-1001"),
            user_id=42,
            chat_id=-1001,
            is_group=True,
            group_id=-1001,
            message_id=100,
            message_thread_id=None,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-group-context",
        tool_name="fetch_group_context",
        effect_kind="read.fetch_group_context",
        mutating=False,
        arguments={"window_size": 16},
        request_hash="a" * 64,
    )


class WorkspaceAttachmentGroupContextTests(unittest.TestCase):
    """@brief 观察性群媒体绝不伪装为 Workspace 文件 / Observed group media never masquerades as a Workspace file."""

    def test_group_media_caption_and_filename_never_reach_the_tool_result(self) -> None:
        """@brief observer→reader→工具结果只保留非可执行标记 / Observer→reader→tool result retains only a non-actionable marker.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 走完整观察投影与工具读取链路 / Exercise the full observer projection and tool-read chain.

            @return None / None.
            """

            forbidden_caption = "caption -- never expose to the Agent"
            forbidden_file_name = "host-path-looking-name.sh"
            forbidden_file_id = "telegram-capability-never-model-visible"
            updates = (
                _update(
                    update_id=1,
                    message_id=1,
                    media={
                        "photo": [
                            {
                                "file_id": forbidden_file_id,
                                "file_unique_id": "photo-unique",
                            }
                        ],
                        "caption": forbidden_caption,
                    },
                ),
                _update(
                    update_id=2,
                    message_id=2,
                    media={
                        "sticker": {
                            "file_id": forbidden_file_id,
                            "file_unique_id": "sticker-unique",
                            "emoji": "secret sticker emoji",
                        },
                        "caption": forbidden_caption,
                    },
                ),
                _update(
                    update_id=3,
                    message_id=3,
                    media={
                        "document": {
                            "file_id": forbidden_file_id,
                            "file_unique_id": "document-unique",
                            "file_name": forbidden_file_name,
                        },
                        "caption": forbidden_caption,
                    },
                ),
            )
            messages = tuple(_as_group_message(update) for update in updates)
            self.assertTrue(
                all(message.content == _GROUP_ATTACHMENT_MARKER for message in messages)
            )

            result: JsonValue = await fetch_group_context(
                _request(),
                groups=_Groups(messages),
            )
            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertIn(_GROUP_ATTACHMENT_MARKER, serialized)
            self.assertNotIn("<workspace_file", serialized)
            for forbidden in (
                forbidden_caption,
                forbidden_file_name,
                forbidden_file_id,
                "secret sticker emoji",
            ):
                self.assertNotIn(forbidden, serialized)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
