"""@brief 上下文领域模型 / Context domain model."""

from .builder import (
    build_context_state,
    build_tool_context,
    compose_system_prompt,
    create_runtime_replacement,
    render_chat_message,
    render_conversation_scope,
    render_scheduled_task,
    render_user_state,
)
from .models import (
    ChatMessageContext,
    ContextState,
    ConversationScope,
    RuntimeMessageReplacement,
    ScheduledTaskContext,
    UserState,
)

__all__ = [
    "ChatMessageContext",
    "ConversationScope",
    "ContextState",
    "RuntimeMessageReplacement",
    "ScheduledTaskContext",
    "UserState",
    "build_context_state",
    "build_tool_context",
    "compose_system_prompt",
    "create_runtime_replacement",
    "render_chat_message",
    "render_conversation_scope",
    "render_scheduled_task",
    "render_user_state",
]
