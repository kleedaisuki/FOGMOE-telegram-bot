"""Assistant 群上下文 operation / Assistant group-context operation."""

from collections.abc import Sequence
from typing import Protocol

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.chat.group_messages import GroupMessage
from fogmoe_bot.domain.conversation.payloads import JsonValue

from .parsing import bounded_int


class GroupContextReader(Protocol):
    """读取当前消息之前 canonical group projection 的窄端口。"""

    async def fetch_before(
        self,
        group_id: int,
        *,
        before_message_id: int | None,
        limit: int,
    ) -> Sequence[GroupMessage]:
        """读取有界群消息窗口 / Read a bounded group-message window."""

        ...


async def fetch_group_context(
    request: ToolEffectRequest,
    *,
    groups: GroupContextReader,
) -> JsonValue:
    """读取当前消息之前的 canonical 群上下文。"""

    group_id = request.context.group_id
    if not request.context.is_group or group_id is None:
        return {"error": "This tool is available only in a group chat"}
    window_size = bounded_int(
        request.arguments,
        "window_size",
        minimum=1,
        maximum=100,
        default=10,
    )
    messages = await groups.fetch_before(
        group_id,
        before_message_id=request.context.message_id,
        limit=window_size,
    )
    return {
        "group_id": group_id,
        "before_message_id": request.context.message_id,
        "window_size": window_size,
        "messages": [
            {
                "message_id": message.message_id,
                "user_id": message.sender_user_id,
                "username": message.sender_name,
                "message_type": message.kind.value,
                "content": message.content,
                "created_at": message.created_at.isoformat(),
                "edited": message.edited,
            }
            for message in messages
        ],
    }
