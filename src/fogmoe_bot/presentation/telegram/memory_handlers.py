"""@brief Telegram Memory 与 User Profile 管理命令 / Telegram commands for Memory and User Profile management."""

from __future__ import annotations

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.memory import ForgetMemory, MemoryForgetPersistence
from fogmoe_bot.application.telegram import DurableGroupAdministratorAuthorization
from fogmoe_bot.application.user_profile import (
    ClearUserProfile,
    RequestUserProfileRegeneration,
    UserProfileManagementPersistence,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId, TurnSource
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.retrieval import RetrievalScope

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import delivery_stream_for_chat


_COMMANDS = frozenset({"resetmem", "resetprofile", "regen", "resetgroup"})
"""@brief 本 handler 独占的命令 / Commands exclusively owned by this handler."""

_SUCCESS_TEXT = {
    "resetmem": (
        "好啦，你的个人记忆已经清空，收进雾里啦。\n"
        "之后我不会再从那些旧记忆里找线索；当前对话和 User Profile 还在，"
        "才、才没有把你全忘掉呢。\n"
        "Alright, your personal memories have been cleared and tucked into the mist.\n"
        "I won't look through those old memories anymore; your current conversation and "
        "User Profile are still here—I didn't forget everything about you, okay?"
    ),
    "resetprofile": (
        "User Profile 已清除。之后只会根据本次操作后的新私聊重新形成画像；"
        "当前上下文与检索记忆不受影响。\n"
        "User Profile has been cleared and will rebuild only from newer private chats."
    ),
    "regen": (
        "已请求后台更新 User Profile；存在尚未归纳的新私聊时会异步处理。\n"
        "A background User Profile refresh has been requested."
    ),
    "resetgroup": (
        "这个群的共享记忆已经清空，悄悄散进雾里了。\n"
        "大家各自的个人记忆和 User Profile 都没有动到，别担心，"
        "我不会把谁弄丢的。\n"
        "This group's shared memories have been cleared and quietly drifted into the mist.\n"
        "Everyone's personal memories and User Profiles are untouched; don't worry, "
        "I won't lose anyone."
    ),
}
"""@brief 各状态变更的稳定确认文本 / Stable confirmation text for each state transition."""

_GROUP_ONLY_TEXT = (
    "唔，这个小法术只能在群组或超级群组里施放哦。\n"
    "This little spell only works in a group or supergroup."
)
"""@brief `/resetgroup` 私聊反馈 / Private-chat feedback for `/resetgroup`."""

_GROUP_ADMIN_ONLY_TEXT = (
    "不行哦，只有本群的 owner 或管理员才能把共享记忆收进雾里。\n"
    "Only this group's owner or an administrator may tuck its shared memories into the mist."
)
"""@brief 群管理员授权拒绝反馈 / Group-administrator authorization denial."""

_NO_ARGUMENTS_TEXT = (
    "这个指令不用带参数啦，直接发送就好。\n"
    "This command takes no arguments; just send it on its own."
)
"""@brief 非空参数反馈 / Feedback for unexpected arguments."""


class MemoryManagementTelegramCommandHandler:
    """@brief 将四个状态管理命令映射到独立应用端口 / Map four state-management commands to separate application ports."""

    def __init__(
        self,
        *,
        memories: MemoryForgetPersistence,
        profiles: UserProfileManagementPersistence,
        group_authorization: DurableGroupAdministratorAuthorization,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入 Memory、Profile、授权与反馈能力 / Inject Memory, Profile, authorization, and feedback capabilities.

        @param memories Memory 遗忘 UoW / Memory-forgetting UoW.
        @param profiles Profile 管理 UoW / Profile-management UoW.
        @param group_authorization durable 群管理员授权 / Durable group-administrator authorization.
        @param outbound 非状态变更反馈 outbox / Outbox for non-mutating feedback.
        """

        self._memories = memories
        self._profiles = profiles
        self._group_authorization = group_authorization
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回命令所有权 / Return command ownership.

        @return 四个无 slash 命令 / Four commands without slashes.
        """

        return _COMMANDS

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行一个幂等管理命令 / Execute one idempotent management command.

        @param update durable Update / Durable Update.
        @param command 类型化 Telegram command / Typed Telegram command.
        @return None / None.
        @raise ValueError 收到非本 handler 命令 / An unowned command is received.
        """

        if command.command not in _COMMANDS:
            raise ValueError("Memory-management handler received an unowned command")
        if command.argument_text:
            await self._feedback(update, command, _NO_ARGUMENTS_TEXT)
            return
        if command.command == "resetgroup":
            await self._reset_group(update, command)
            return

        confirmation = _confirmation(update, command, _SUCCESS_TEXT[command.command])
        source = TurnSource.telegram(update.update_id)
        if command.command == "resetmem":
            await self._memories.forget(
                ForgetMemory(
                    source=source,
                    conversation_id=update.conversation_id,
                    scope=RetrievalScope("personal", command.user_id),
                    confirmation=confirmation,
                    requested_at=update.received_at,
                )
            )
        elif command.command == "resetprofile":
            await self._profiles.clear(
                ClearUserProfile(
                    source=source,
                    conversation_id=update.conversation_id,
                    user_id=command.user_id,
                    confirmation=confirmation,
                    requested_at=update.received_at,
                )
            )
        else:
            await self._profiles.request_regeneration(
                RequestUserProfileRegeneration(
                    source=source,
                    conversation_id=update.conversation_id,
                    user_id=command.user_id,
                    confirmation=confirmation,
                    requested_at=update.received_at,
                )
            )

    async def _reset_group(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 校验群作用域与 durable 管理员决定后遗忘 / Forget after validating group scope and durable administrator authorization.

        @param update durable Update / Durable Update.
        @param command `/resetgroup` envelope / `/resetgroup` envelope.
        @return None / None.
        """

        if command.chat_type not in {"group", "supergroup"}:
            await self._feedback(update, command, _GROUP_ONLY_TEXT)
            return
        allowed = await self._group_authorization.authorize(
            update_id=update.update_id,
            chat_id=command.chat_id,
            actor_user_id=command.user_id,
            observed_at=update.received_at,
        )
        if not allowed:
            await self._feedback(update, command, _GROUP_ADMIN_ONLY_TEXT)
            return
        await self._memories.forget(
            ForgetMemory(
                source=TurnSource.telegram(update.update_id),
                conversation_id=update.conversation_id,
                scope=RetrievalScope("group", command.chat_id),
                confirmation=_confirmation(
                    update,
                    command,
                    _SUCCESS_TEXT[command.command],
                ),
                requested_at=update.received_at,
            )
        )

    async def _feedback(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        text: str,
    ) -> None:
        """@brief 幂等写入不触发状态变更的反馈 / Idempotently write feedback that performs no state mutation.

        @param update durable Update / Durable Update.
        @param command command envelope / Command envelope.
        @param text 稳定反馈文本 / Stable feedback text.
        @return None / None.
        """

        await self._outbound.enqueue(
            StandaloneOutboundCommand(
                conversation_id=update.conversation_id,
                delivery_stream_id=delivery_stream_for_chat(
                    command.chat_id,
                    command.message_thread_id,
                ),
                kind=SEND_TELEGRAM_MESSAGE,
                payload=_payload(command, text),
                idempotency_key=_response_key(update, command),
                created_at=update.received_at,
            )
        )


def _confirmation(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
    text: str,
) -> OutboundDraft:
    """@brief 构造与状态变更原子提交的确认 / Build the confirmation committed atomically with a state transition.

    @param update durable Update / Durable Update.
    @param command command envelope / Command envelope.
    @param text 确认文本 / Confirmation text.
    @return 确定性 standalone draft / Deterministic standalone draft.
    """

    key = _response_key(update, command)
    return OutboundDraft(
        message_id=OutboundMessageId.for_conversation(update.conversation_id, key),
        conversation_id=update.conversation_id,
        turn_id=None,
        delivery_stream_id=delivery_stream_for_chat(
            command.chat_id,
            command.message_thread_id,
        ),
        kind=SEND_TELEGRAM_MESSAGE,
        payload=_payload(command, text),
        idempotency_key=key,
        created_at=update.received_at,
        trace_context=update.trace_context,
    )


def _payload(command: ParsedTelegramCommand, text: str) -> JsonObject:
    """@brief 渲染 Telegram send-message payload / Render a Telegram send-message payload.

    @param command command envelope / Command envelope.
    @param text 纯文本内容 / Plain-text content.
    @return connector payload / Connector payload.
    """

    return {
        "chat_id": command.chat_id,
        "text": text,
        "message_thread_id": command.message_thread_id,
        "reply_to_message_id": command.message_id,
        "disable_web_page_preview": True,
    }


def _response_key(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
) -> str:
    """@brief 派生唯一响应幂等键 / Derive the unique response idempotency key.

    @param update durable Update / Durable Update.
    @param command command envelope / Command envelope.
    @return 稳定 key / Stable key.
    """

    return f"update:{int(update.update_id)}:command:{command.command}:response"


__all__ = ["MemoryManagementTelegramCommandHandler"]
