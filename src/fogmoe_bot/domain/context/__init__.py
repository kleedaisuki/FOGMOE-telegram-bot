"""@brief 上下文领域模型 / Context domain model."""

from .builder import (
    build_model_query,
    build_tool_context,
    compose_system_prompt,
    create_runtime_replacement,
    render_chat_message,
    render_scheduled_task,
    render_user_state,
)
from .models import (
    ChatMessageContext,
    ConversationScope,
    ModelQuery,
    RuntimeMessageReplacement,
    ScheduledTaskContext,
    UserState,
)

__all__ = [
    "ChatMessageContext",
    "ConversationScope",
    "ModelQuery",
    "RuntimeMessageReplacement",
    "ScheduledTaskContext",
    "UserState",
    "build_model_query",
    "build_tool_context",
    "compose_system_prompt",
    "create_runtime_replacement",
    "render_chat_message",
    "render_scheduled_task",
    "render_user_state",
]
