"""@brief 纯上下文转换工具 / Pure context transformation utilities."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from uuid import UUID

from .formatting import (
    format_metadata_attrs,
    format_user_state_prompt,
    join_prompt_sections,
    xml_escape,
)
from .models import (
    ChatMessageContext,
    ContextState,
    ConversationScope,
    RuntimeMessageReplacement,
    ScheduledTaskContext,
    UserState,
)


def compose_system_prompt(*, system_prompt: str, user_state_prompt: str) -> str:
    """@brief 组合系统提示词 / Compose system prompt.

    @param system_prompt 静态系统策略 / Static system policy.
    @param user_state_prompt 当前用户状态片段 / Current user-state fragment.
    @return 静态策略与运行时状态组成的系统提示词 / Composed system prompt.
    """
    return join_prompt_sections(system_prompt, user_state_prompt)


def render_user_state(user_state: UserState) -> str:
    """@brief 渲染用户状态提示词 / Render user state prompt.

    @param user_state 已加载的用户状态 / Loaded user state.
    @return 用户状态提示词 / User-state prompt.
    """
    return format_user_state_prompt(
        user_coins=user_state.coins,
        user_plan=user_state.plan,
        user_permission=user_state.permission,
        profile=user_state.profile,
        personal_info=user_state.personal_info,
        diary_exists=user_state.diary_exists,
        user_id=user_state.user_id,
        username=user_state.username,
        display_name=user_state.display_name,
    )


def render_conversation_scope(scope: ConversationScope) -> str:
    """@brief 渲染可信的 Context 作用域 / Render the trusted Context scope.

    @param scope 当前 Conversation 作用域 / Current Conversation scope.
    @return 私聊或共享群 Topic 标记 / Private or shared group-topic marker.
    """

    if not scope.is_group:
        return (
            '<conversation_scope kind="private" shared="false" '
            f'current_user_id="{scope.user_id}" />'
        )
    if scope.group_id is None:
        raise ValueError("Group ConversationScope requires group_id")
    thread = scope.message_thread_id or 0
    return (
        '<conversation_scope kind="group" shared="true" '
        f'group_id="{scope.group_id}" thread_id="{thread}" '
        f'current_user_id="{scope.user_id}" />'
    )


def render_chat_message(context: ChatMessageContext) -> str:
    """@brief 渲染聊天消息 / Render chat message.

    @param context 聊天消息上下文 / Chat message context.
    @return XML-like 消息文本 / XML-like message text.
    """
    attrs = [
        ("type", context.chat_type),
        ("timestamp", context.timestamp),
        ("user", context.user_name),
        ("username", f"@{context.username}" if context.username else None),
        ("user_id", str(context.user_id) if context.user_id is not None else None),
        (
            "thread_id",
            str(context.message_thread_id)
            if context.message_thread_id is not None
            else None,
        ),
        (
            "message_id",
            str(context.message_id) if context.message_id is not None else None,
        ),
        ("edited", "true" if context.edited else None),
        ("edited_at", context.edited_at if context.edited else None),
    ]
    if context.chat_type in ("group", "supergroup") and context.chat_title:
        attrs.insert(1, ("title", context.chat_title))

    lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
    _append_forward(lines, context)
    _append_reply(lines, context)
    _append_media(lines, context)
    lines.append("</metadata>")
    lines.append(f"<message>{xml_escape(context.message_text)}</message>")
    return "\n".join(lines)


def render_scheduled_task(context: ScheduledTaskContext) -> str:
    """@brief 渲染定时任务事件 / Render scheduled task event.

    @param context 定时任务上下文 / Scheduled task context.
    @return XML-like 定时任务事件 / XML-like scheduled task event.
    """
    attrs = [
        ("type", "scheduler"),
        ("timestamp", _format_datetime(context.timestamp)),
        ("origin", "scheduled_task"),
    ]
    if context.scheduled_at:
        attrs.append(("scheduled_at", _format_datetime(context.scheduled_at)))
    if context.scheduled_for:
        attrs.append(("scheduled_for", _format_datetime(context.scheduled_for)))

    lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
    lines.append(f"  <trigger>{xml_escape(context.trigger_reason)}</trigger>")
    if context.context_text:
        lines.append(f"  <context>{xml_escape(context.context_text)}</context>")
    lines.append(f"  <instruction>{xml_escape(context.instruction)}</instruction>")
    lines.append("</metadata>")
    return "\n".join(lines)


def create_runtime_replacement(
    *,
    persisted_content: str,
    runtime_message: dict[str, object] | None,
) -> RuntimeMessageReplacement | None:
    """@brief 创建运行时消息替换 / Create a runtime message replacement.

    @param persisted_content 持久化内容 / Persisted content.
    @param runtime_message 运行时消息 / Runtime message.
    @return 替换对象；运行时消息为空时返回 None / Replacement or None.
    """
    if runtime_message is None:
        return None
    return RuntimeMessageReplacement(
        persisted_content=persisted_content,
        runtime_message=runtime_message,
    )


def build_context_state(
    *,
    context_id: UUID,
    system_prompt: str,
    history_messages: Iterable[Mapping[str, object]],
    scope: ConversationScope,
    user_state: UserState,
    runtime_replacements: Iterable[RuntimeMessageReplacement] | None = None,
    text_fallback_messages: Iterable[Mapping[str, object]] | None = None,
) -> ContextState:
    """@brief 构造 Agent 上下文状态 / Build Agent context state.

    @param context_id ContextState 实体标识 / ContextState entity identifier.
    @param system_prompt 静态系统策略 / Static system policy.
    @param history_messages 当前会话历史 / Current conversation history.
    @param scope 对话作用域 / Conversation scope.
    @param user_state 本回合用户状态 / User state for this turn.
    @param runtime_replacements 运行时消息替换 / Runtime message replacements.
    @param text_fallback_messages 纯文本降级历史 / Text-only fallback history.
    @return 可直接交给 AgentLoop 的领域上下文快照 / Domain context snapshot ready for AgentLoop.
    @raise ValueError 群聊携带私人 Profile 状态 / A group scope carries private Profile state.
    """
    if scope.is_group and (
        user_state.profile is not None
        or bool(user_state.personal_info)
        or user_state.diary_exists
    ):
        raise ValueError(
            "Group ContextState cannot contain private User Profile, personal_info, or diary state"
        )
    history = [
        dict(message) for message in history_messages if isinstance(message, Mapping)
    ]
    query_history = _apply_runtime_replacements(
        history, list(runtime_replacements or [])
    )
    system_message: dict[str, object] = {
        "role": "system",
        "content": compose_system_prompt(
            system_prompt=system_prompt,
            user_state_prompt=join_prompt_sections(
                render_conversation_scope(scope),
                render_user_state(user_state),
            ),
        ),
    }
    fallback: list[dict[str, object]] | None = None
    if text_fallback_messages is not None:
        fallback_history = [
            dict(message)
            for message in text_fallback_messages
            if isinstance(message, Mapping)
        ]
        fallback = [dict(system_message), *fallback_history]

    return ContextState(
        context_id=context_id,
        scope=scope,
        user_state=user_state,
        messages=[system_message, *query_history],
        tool_context=build_tool_context(scope),
        text_fallback_messages=fallback,
    )


def build_tool_context(scope: ConversationScope) -> dict[str, object]:
    """@brief 构造工具请求上下文 / Build tool request context.

    @param scope 对话作用域 / Conversation scope.
    @return 工具上下文字典 / Tool context dictionary.
    """
    return {
        "is_group": scope.is_group,
        "group_id": scope.group_id,
        "message_id": scope.message_id,
        "message_thread_id": scope.message_thread_id,
        "user_id": scope.user_id,
    }


def _apply_runtime_replacements(
    messages: list[dict[str, object]],
    replacements: list[RuntimeMessageReplacement],
) -> list[dict[str, object]]:
    """@brief 应用运行时消息替换 / Apply runtime message replacements.

    @param messages 历史消息 / History messages.
    @param replacements 替换列表 / Replacement list.
    @return 模型消息链 / Model message chain.
    """
    if not replacements:
        return list(messages)

    messages_for_model = list(messages)
    search_end = len(messages_for_model) - 1
    for replacement in reversed(replacements):
        for index in range(search_end, -1, -1):
            message = messages_for_model[index]
            if (
                message.get("role") == "user"
                and message.get("content") == replacement.persisted_content
            ):
                messages_for_model[index] = dict(replacement.runtime_message)
                search_end = index - 1
                break
    return messages_for_model


def _append_forward(lines: list[str], context: ChatMessageContext) -> None:
    """@brief 追加转发元数据 / Append forward metadata.

    @param lines 输出行列表 / Output lines.
    @param context 聊天消息上下文 / Chat message context.
    """
    if not context.forward_type:
        return
    forward_attr_text = _format_optional_attrs(
        [
            ("type", context.forward_type),
            ("origin_timestamp", context.forward_origin_timestamp),
            ("user", context.forward_user),
            ("name", context.forward_name),
            ("chat", context.forward_chat),
            ("message_id", context.forward_message_id),
            ("author_signature", context.forward_author_signature),
        ]
    )
    lines.append(f"  <forward {forward_attr_text} />")


def _append_reply(lines: list[str], context: ChatMessageContext) -> None:
    """@brief 追加回复元数据 / Append reply metadata.

    @param lines 输出行列表 / Output lines.
    @param context 聊天消息上下文 / Chat message context.
    """
    if context.reply_type:
        reply_user_value = f"@{context.reply_user}" if context.reply_user else ""
        reply_attr_text = _format_optional_attrs(
            [
                ("user", reply_user_value),
                ("type", context.reply_type),
                ("emoji", context.reply_emoji),
            ]
        )
        lines.append(f"  <reply {reply_attr_text}>")
        if context.reply_text:
            lines.append(f"    <text>{xml_escape(context.reply_text)}</text>")
        if context.reply_caption:
            lines.append(f"    <caption>{xml_escape(context.reply_caption)}</caption>")
        if context.reply_summary:
            lines.append(f"    <summary>{xml_escape(context.reply_summary)}</summary>")
        lines.append("  </reply>")
        return

    if context.reply_user or context.reply_text:
        reply_user_value = f"@{context.reply_user}" if context.reply_user else ""
        reply_attr = (
            f' user="{xml_escape(reply_user_value)}"' if reply_user_value else ""
        )
        lines.append(
            f"  <reply{reply_attr}>{xml_escape(context.reply_text or '')}</reply>"
        )


def _append_media(lines: list[str], context: ChatMessageContext) -> None:
    """@brief 追加媒体元数据 / Append media metadata.

    @param lines 输出行列表 / Output lines.
    @param context 聊天消息上下文 / Chat message context.
    """
    if not context.media_type:
        return
    media_attrs: list[tuple[str, str | None]] = [("type", context.media_type)]
    if context.media_emoji:
        media_attrs.append(("emoji", context.media_emoji))
    lines.append(f"  <media {_format_optional_attrs(media_attrs)}>")
    if context.media_description:
        lines.append(
            f"    <description>{xml_escape(context.media_description)}</description>"
        )
    lines.append("  </media>")


def _format_optional_attrs(attrs: list[tuple[str, str | None]]) -> str:
    """@brief 格式化可选 XML 属性 / Format optional XML attributes.

    @param attrs 属性键值对 / Attribute key-value pairs.
    @return 属性文本 / Attribute text.
    """
    return " ".join(f'{key}="{xml_escape(value)}"' for key, value in attrs if value)


def _format_datetime(value: datetime | None) -> str:
    """@brief 格式化时间 / Format datetime.

    @param value 时间值 / Datetime value.
    @return UTC 无时区时间文本 / UTC naive timestamp text.
    """
    if not value:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")
