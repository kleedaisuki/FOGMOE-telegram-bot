from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from fogmoe_bot.domain.conversation.prompt_utils import (
    format_metadata_attrs,
    format_user_state_prompt,
    xml_escape,
)

from .models import (
    ChatMessageContext,
    ConversationScope,
    ModelQuery,
    RuntimeMessageReplacement,
    ScheduledTaskContext,
    UserState,
)


class ContextBuilder:
    """@brief 构造模型上下文 / Build model-facing context.

    @note 该类只处理纯领域渲染和模型 query 组装，不访问 Telegram、数据库或 LLM provider。
    / This class only renders domain context and assembles model queries; it does not access Telegram, databases, or LLM providers.
    """

    def render_user_state(self, user_state: UserState) -> str:
        """@brief 渲染用户状态提示词 / Render user state prompt.

        @param user_state 用户状态 / User state.
        @return 用户状态提示词 / User state prompt.
        """

        return format_user_state_prompt(
            user_coins=user_state.coins,
            user_plan=user_state.plan,
            user_permission=user_state.permission,
            impression=user_state.impression,
            personal_info=user_state.personal_info,
            diary_exists=user_state.diary_exists,
        )

    def render_chat_message(self, context: ChatMessageContext) -> str:
        """@brief 渲染聊天消息 / Render chat message.

        @param context 聊天消息上下文 / Chat message context.
        @return XML-like 消息文本 / XML-like message text.
        """

        attrs = [
            ("type", context.chat_type),
            ("timestamp", context.timestamp),
            ("user", f"@{context.user_name}"),
            ("message_id", str(context.message_id) if context.message_id is not None else None),
            ("edited", "true" if context.edited else None),
            ("edited_at", context.edited_at if context.edited else None),
        ]
        if context.chat_type in ("group", "supergroup") and context.chat_title:
            attrs.insert(1, ("title", context.chat_title))

        lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
        self._append_forward(lines, context)
        self._append_reply(lines, context)
        self._append_media(lines, context)
        lines.append("</metadata>")
        lines.append(f"<message>{xml_escape(context.message_text)}</message>")
        return "\n".join(lines)

    def render_scheduled_task(self, context: ScheduledTaskContext) -> str:
        """@brief 渲染定时任务事件 / Render scheduled task event.

        @param context 定时任务上下文 / Scheduled task context.
        @return XML-like 定时任务事件 / XML-like scheduled task event.
        """

        attrs = [
            ("type", "scheduler"),
            ("timestamp", self._format_datetime(context.timestamp)),
            ("origin", "scheduled_task"),
        ]
        if context.scheduled_at:
            attrs.append(("scheduled_at", self._format_datetime(context.scheduled_at)))
        if context.scheduled_for:
            attrs.append(("scheduled_for", self._format_datetime(context.scheduled_for)))

        lines = [f"<metadata {format_metadata_attrs(attrs)}>"]
        lines.append(f"  <trigger>{xml_escape(context.trigger_reason)}</trigger>")
        if context.context_text:
            lines.append(f"  <context>{xml_escape(context.context_text)}</context>")
        lines.append(f"  <instruction>{xml_escape(context.instruction)}</instruction>")
        lines.append("</metadata>")
        return "\n".join(lines)

    def create_runtime_replacement(
        self,
        *,
        persisted_content: str,
        runtime_message: dict[str, Any] | None,
    ) -> RuntimeMessageReplacement | None:
        """@brief 创建运行时消息替换 / Create a runtime message replacement.

        @param persisted_content 持久化内容 / Persisted content.
        @param runtime_message 运行时消息 / Runtime message.
        @return 替换对象；运行时消息为空时返回 None / Replacement object, or None when runtime message is empty.
        """

        if runtime_message is None:
            return None
        return RuntimeMessageReplacement(
            persisted_content=persisted_content,
            runtime_message=runtime_message,
        )

    def build_model_query(
        self,
        *,
        history_messages: Iterable[Mapping[str, Any]],
        scope: ConversationScope,
        user_state_prompt: str,
        runtime_replacements: Iterable[RuntimeMessageReplacement] | None = None,
        text_fallback_messages: Iterable[Mapping[str, Any]] | None = None,
    ) -> ModelQuery:
        """@brief 构造模型推理查询 / Build a model inference query.

        @param history_messages 当前会话历史 / Current conversation history.
        @param scope 对话作用域 / Conversation scope.
        @param user_state_prompt 用户状态提示词 / User state prompt.
        @param runtime_replacements 运行时消息替换 / Runtime message replacements.
        @param text_fallback_messages 纯文本降级历史 / Text-only fallback history.
        @return 可直接交给模型 router 的查询 / Query ready for the model router.
        """

        history = [dict(message) for message in history_messages if isinstance(message, Mapping)]
        replacements = list(runtime_replacements or [])
        query_messages = self._apply_runtime_replacements(history, replacements)
        fallback = None
        if text_fallback_messages is not None:
            fallback = [
                dict(message)
                for message in text_fallback_messages
                if isinstance(message, Mapping)
            ]

        return ModelQuery(
            messages=query_messages,
            tool_context=self.build_tool_context(
                scope,
                user_state_prompt=user_state_prompt,
            ),
            text_fallback_messages=fallback,
        )

    def build_tool_context(
        self,
        scope: ConversationScope,
        *,
        user_state_prompt: str,
    ) -> dict[str, Any]:
        """@brief 构造工具请求上下文 / Build tool request context.

        @param scope 对话作用域 / Conversation scope.
        @param user_state_prompt 用户状态提示词 / User state prompt.
        @return 工具上下文字典 / Tool context dictionary.
        """

        return {
            "is_group": scope.is_group,
            "group_id": scope.group_id,
            "message_id": scope.message_id,
            "user_id": scope.user_id,
            "user_state_prompt": user_state_prompt,
        }

    def _apply_runtime_replacements(
        self,
        messages: list[dict[str, Any]],
        replacements: list[RuntimeMessageReplacement],
    ) -> list[dict[str, Any]]:
        """@brief 应用运行时消息替换 / Apply runtime message replacements.

        @param messages 历史消息 / History messages.
        @param replacements 替换列表 / Replacements.
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

    def _append_forward(self, lines: list[str], context: ChatMessageContext) -> None:
        """@brief 追加转发元数据 / Append forward metadata.

        @param lines 输出行列表 / Output lines.
        @param context 聊天消息上下文 / Chat message context.
        """

        if not context.forward_type:
            return
        forward_attr_text = self._format_optional_attrs(
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

    def _append_reply(self, lines: list[str], context: ChatMessageContext) -> None:
        """@brief 追加回复元数据 / Append reply metadata.

        @param lines 输出行列表 / Output lines.
        @param context 聊天消息上下文 / Chat message context.
        """

        if context.reply_type:
            reply_user_value = f"@{context.reply_user}" if context.reply_user else ""
            reply_attr_text = self._format_optional_attrs(
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
            reply_attr = f' user="{xml_escape(reply_user_value)}"' if reply_user_value else ""
            lines.append(f"  <reply{reply_attr}>{xml_escape(context.reply_text or '')}</reply>")

    def _append_media(self, lines: list[str], context: ChatMessageContext) -> None:
        """@brief 追加媒体元数据 / Append media metadata.

        @param lines 输出行列表 / Output lines.
        @param context 聊天消息上下文 / Chat message context.
        """

        if not context.media_type:
            return
        media_attrs = [("type", context.media_type)]
        if context.media_emoji:
            media_attrs.append(("emoji", context.media_emoji))
        media_attr_text = self._format_optional_attrs(media_attrs)
        lines.append(f"  <media {media_attr_text}>")
        if context.media_description:
            lines.append(
                f"    <description>{xml_escape(context.media_description)}</description>"
            )
        lines.append("  </media>")

    def _format_optional_attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        """@brief 格式化可选 XML 属性 / Format optional XML attributes.

        @param attrs 属性键值对 / Attribute key-value pairs.
        @return 属性文本 / Attribute text.
        """

        return " ".join(
            f'{key}="{xml_escape(value)}"' for key, value in attrs if value
        )

    def _format_datetime(self, value: datetime | None) -> str:
        """@brief 格式化时间 / Format datetime.

        @param value 时间值 / Datetime value.
        @return UTC 无时区时间文本 / UTC naive timestamp text.
        """

        if not value:
            return ""
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%d %H:%M:%S")


DEFAULT_CONTEXT_BUILDER = ContextBuilder()
