from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .formatting import format_user_state_prompt, join_prompt_sections


@dataclass(frozen=True)
class UserState:
    """@brief 用户运行时状态 / Runtime user state.

    @param coins 用户当前硬币数 / Current user coin balance.
    @param plan 用户订阅计划 / User subscription plan.
    @param permission 用户权限等级 / User permission level.
    @param impression 助手对用户的长期印象 / Assistant long-term impression of the user.
    @param personal_info 用户自定义个人信息 / User-defined personal information.
    @param diary_exists 是否存在用户日记 / Whether user diary exists.
    """

    coins: int
    plan: str
    permission: int
    impression: str
    personal_info: str = ""
    diary_exists: bool = False


@dataclass(frozen=True)
class ChatMessageContext:
    """@brief 聊天消息上下文 / Chat message context.

    @param chat_type Telegram 聊天类型 / Telegram chat type.
    @param chat_title 群聊标题 / Group chat title.
    @param timestamp 消息时间戳文本 / Message timestamp text.
    @param user_name Telegram 用户名 / Telegram username.
    @param message_text 用户消息正文 / User message body.
    @param message_id Telegram 消息 ID / Telegram message id.
    @param edited 是否为编辑消息 / Whether the message is edited.
    @param edited_at 编辑时间 / Edit timestamp.
    @param forward_type 转发来源类型 / Forward origin type.
    @param forward_origin_timestamp 转发来源时间 / Forward origin timestamp.
    @param forward_user 转发来源用户 / Forward origin user.
    @param forward_name 转发来源名称 / Forward origin name.
    @param forward_chat 转发来源聊天 / Forward origin chat.
    @param forward_message_id 转发来源消息 ID / Forward origin message id.
    @param forward_author_signature 转发作者签名 / Forward author signature.
    @param reply_user 被回复用户 / Replied user.
    @param reply_text 被回复文本 / Replied text.
    @param reply_type 被回复消息类型 / Replied message type.
    @param reply_caption 被回复媒体说明 / Replied media caption.
    @param reply_summary 被回复媒体摘要 / Replied media summary.
    @param reply_emoji 被回复贴纸表情 / Replied sticker emoji.
    @param media_type 媒体类型 / Media type.
    @param media_description 媒体描述 / Media description.
    @param media_emoji 媒体表情 / Media emoji.
    """

    chat_type: str
    chat_title: str | None
    timestamp: str
    user_name: str
    message_text: str
    message_id: str | int | None = None
    edited: bool = False
    edited_at: str | None = None
    forward_type: str | None = None
    forward_origin_timestamp: str | None = None
    forward_user: str | None = None
    forward_name: str | None = None
    forward_chat: str | None = None
    forward_message_id: str | None = None
    forward_author_signature: str | None = None
    reply_user: str | None = None
    reply_text: str | None = None
    reply_type: str | None = None
    reply_caption: str | None = None
    reply_summary: str | None = None
    reply_emoji: str | None = None
    media_type: str | None = None
    media_description: str | None = None
    media_emoji: str | None = None


@dataclass(frozen=True)
class ScheduledTaskContext:
    """@brief 定时任务上下文 / Scheduled task context.

    @param timestamp 触发时刻 / Trigger timestamp.
    @param scheduled_at 任务创建时刻 / Schedule creation timestamp.
    @param scheduled_for 计划触发时刻 / Planned trigger timestamp.
    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 创建任务时的上下文 / Context captured when scheduling.
    @param instruction 执行指令 / Execution instruction.
    """

    timestamp: datetime
    scheduled_at: datetime | None
    scheduled_for: datetime | None
    trigger_reason: str
    context_text: str | None
    instruction: str


@dataclass(frozen=True)
class ConversationScope:
    """@brief 对话作用域 / Conversation scope.

    @param user_id Telegram 用户 ID / Telegram user id.
    @param is_group 是否来自群聊 / Whether the turn is from a group chat.
    @param group_id 群聊 ID / Group chat id.
    @param message_id 当前消息 ID / Current message id.
    """

    user_id: int
    is_group: bool = False
    group_id: int | None = None
    message_id: int | None = None


@dataclass(frozen=True)
class RuntimeMessageReplacement:
    """@brief 运行时消息替换 / Runtime message replacement.

    @param persisted_content 持久化历史中的文本内容 / Text content persisted in history.
    @param runtime_message 发给模型的运行时消息 / Runtime message sent to the model.
    @note 用于图片等多模态消息：数据库保存可读文本，模型调用可替换为多模态 payload。
    / Used for multimodal messages: the database stores readable text while model calls may use multimodal payloads.
    """

    persisted_content: str
    runtime_message: dict[str, Any]


@dataclass
class ContextState:
    """@brief 可递增的会话领域上下文 / Incremental domain context for one conversation.

    ``ContextState`` 是上层交给 Agent 的唯一内容载体，也是会话缓存保存的工作集。
    调用方必须在会话锁内更新它，并且只能在对应记录成功持久化后把记录加入 ``messages``。
    AgentLoop 可以在一次执行中追加临时工具消息；失败时调用方应丢弃整个缓存项。

    @param scope 本次回合的会话作用域 / Conversation scope for this turn.
    @param user_state 本次回合可见的用户状态 / User state visible to this turn.
    @param messages 已提交或当前回合临时的模型消息链 / Committed or in-turn model message chain.
    @param tool_context 传给 Agent 工具的显式作用域 / Explicit scope passed to Agent tools.
    @param text_fallback_messages 纯文本模型的降级消息链 / Text-only fallback message chain.
    """

    scope: ConversationScope
    user_state: UserState
    messages: list[dict[str, Any]]
    tool_context: dict[str, Any]
    text_fallback_messages: list[dict[str, Any]] | None = None

    def refresh_turn(
        self,
        *,
        system_prompt: str,
        scope: ConversationScope,
        user_state: UserState,
        runtime_replacements: Iterable[RuntimeMessageReplacement] | None = None,
    ) -> None:
        """@brief 刷新缓存会话的本回合投影 / Refresh a cached conversation for one turn.

        @param system_prompt 静态系统策略 / Static system policy.
        @param scope 当前回合作用域 / Current-turn scope.
        @param user_state 当前回合用户状态 / Current-turn user state.
        @param runtime_replacements 仅本次模型调用有效的消息替换 / Replacements valid only for this model call.
        @return None / None.
        @note 调用前 ``messages`` 必须仅含已持久化的规范消息。/
        Before calling, ``messages`` must contain canonical persisted messages only.
        """

        self.scope = scope
        self.user_state = user_state
        self.tool_context = {
            "is_group": scope.is_group,
            "group_id": scope.group_id,
            "message_id": scope.message_id,
            "user_id": scope.user_id,
        }
        system_message = {
            "role": "system",
            "content": join_prompt_sections(
                system_prompt,
                format_user_state_prompt(
                    user_coins=user_state.coins,
                    user_plan=user_state.plan,
                    user_permission=user_state.permission,
                    impression=user_state.impression,
                    personal_info=user_state.personal_info,
                    diary_exists=user_state.diary_exists,
                ),
            ),
        }
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = system_message
        else:
            self.messages.insert(0, system_message)

        self.text_fallback_messages = None
        replacements = list(runtime_replacements or [])
        if replacements:
            self.text_fallback_messages = list(self.messages)
            self.messages = self._apply_runtime_replacements(replacements)

    def append_persisted_records(self, records: Iterable[tuple[str, Any]]) -> None:
        """@brief 将已提交记录增量加入会话 / Append committed records to the conversation.

        @param records 数据库已成功写入的 `(role, content)` 记录 / Successfully persisted records.
        @return None / None.
        @note 仅复制新增记录，以隔离调用方 payload；不复制已有历史。/
        Only new records are copied to isolate caller payloads; existing history is not copied.
        """

        for role, content in records:
            if isinstance(content, Mapping):
                message = deepcopy(dict(content))
                message.setdefault("role", role)
            else:
                message = {"role": role, "content": content}
            self.messages.append(message)

    def discard_runtime_messages(self, *, committed_message_count: int) -> None:
        """@brief 丢弃 Agent 的未提交临时消息 / Discard uncommitted Agent messages.

        @param committed_message_count 保留的规范消息数量 / Number of canonical messages to retain.
        @return None / None.
        """

        if self.text_fallback_messages is not None:
            self.messages = self.text_fallback_messages
        del self.messages[committed_message_count:]
        self.text_fallback_messages = None

    def _apply_runtime_replacements(
        self,
        replacements: list[RuntimeMessageReplacement],
    ) -> list[dict[str, Any]]:
        """@brief 应用本回合消息替换 / Apply current-turn message replacements.

        @param replacements 本回合的多模态替换 / Current-turn multimodal replacements.
        @return 面向模型的消息链 / Model-facing message chain.
        """

        messages_for_model = list(self.messages)
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
