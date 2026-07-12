"""Durable primary route for Telegram Assistant messages."""

from __future__ import annotations

from fogmoe_bot.application.conversation.assistant_ingress import (
    ASSISTANT_TEXT_LIMIT,
    AssistantFeedbackReason,
    AssistantIngressCoordinator,
)
from fogmoe_bot.application.conversation.router import (
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.runtime import WorkPriority
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .assistant_update_models import (
    MalformedTelegramAssistantUpdate,
    TelegramAssistantContentKind,
)
from .assistant_update_parser import (
    looks_like_assistant_candidate,
    parse_telegram_assistant_update,
)


class TelegramAssistantPrimaryRoute:
    """@brief 普通消息与 /fogmoebot 的互斥 durable PrimaryRoute / Exclusive durable PrimaryRoute for ordinary messages and /fogmoebot."""

    def __init__(
        self,
        *,
        coordinator: AssistantIngressCoordinator,
        bot_user_id: int,
        bot_username: str,
    ) -> None:
        """@brief 注入应用协调器与 Bot identity / Inject the application coordinator and Bot identity.

        @param coordinator acceptance/feedback 协调器 / Acceptance and feedback coordinator.
        @param bot_user_id Bot Telegram ID / Bot Telegram ID.
        @param bot_username Bot 用户名 / Bot username.
        """

        if isinstance(bot_user_id, bool) or bot_user_id <= 0:
            raise ValueError("bot_user_id must be positive")
        normalized_username = bot_username.removeprefix("@").strip()
        if not normalized_username:
            raise ValueError("bot_username cannot be blank")
        self._coordinator = coordinator
        self._bot_user_id = bot_user_id
        self._bot_username = normalized_username

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名 / Return the stable route name.

        @return ``telegram-assistant`` / ``telegram-assistant``.
        """

        return "telegram-assistant"

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 判断消息是否唯一属于 Assistant / Check whether the message belongs uniquely to Assistant.

        @param update durable Update / Durable Update.
        @return 普通消息、有效 /fogmoebot 或需隔离的畸形候选为 True /
            True for an ordinary message, valid /fogmoebot, or malformed candidate requiring quarantine.
        """

        try:
            parsed = parse_telegram_assistant_update(update)
        except MalformedTelegramAssistantUpdate:
            return looks_like_assistant_candidate(update.payload)
        return parsed.matches(
            bot_user_id=self._bot_user_id,
            bot_username=self._bot_username,
        )

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造不推理、不直发 Telegram 的幂等操作 / Build an idempotent operation that neither infers nor sends Telegram directly.

        @param update 已匹配 durable Update / Matched durable Update.
        @return keyed runtime 操作 / Keyed-runtime operation.
        """

        parsed = parse_telegram_assistant_update(update)
        if not parsed.matches(
            bot_user_id=self._bot_user_id,
            bot_username=self._bot_username,
        ):
            raise MalformedTelegramAssistantUpdate(
                "Assistant route operation requested for a non-matching Update"
            )
        request = parsed.to_request(update)
        rejection: AssistantFeedbackReason | None = None
        if (
            parsed.content_kind is TelegramAssistantContentKind.TEXT
            and len(parsed.text) > ASSISTANT_TEXT_LIMIT
        ):
            rejection = AssistantFeedbackReason.TEXT_TOO_LONG
        elif parsed.media is not None and parsed.media.declared_too_large:
            rejection = AssistantFeedbackReason.MEDIA_TOO_LARGE

        async def call() -> None:
            """@brief 执行 acceptance 或幂等预检反馈 / Execute acceptance or idempotent preflight feedback.

            @return None / None.
            """

            if rejection is not None:
                await self._coordinator.reject(request, rejection)
                return
            await self._coordinator.handle(request)

        return RoutedOperation(
            name=f"telegram-assistant:{update.update_id.value}",
            key=conversation_aggregate_key(update.conversation_id),
            call=call,
            priority=WorkPriority.HIGH,
        )
