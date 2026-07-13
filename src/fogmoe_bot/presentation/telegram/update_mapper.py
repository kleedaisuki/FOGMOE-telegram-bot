"""@brief Telegram Update 到 durable ingress 模型的映射 / Map Telegram Updates to durable ingress models.

本模块是 Telegram SDK 对象能够出现的最内侧边界。持久化与应用层只接收经过
JSON 校验的 ``InboundUpdate``，不接收 ``telegram.Update``。/
This module is the innermost boundary where Telegram SDK objects may appear. Persistence
and application layers receive only JSON-validated ``InboundUpdate`` values, never
``telegram.Update`` objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from telegram import Update

from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)


@dataclass(frozen=True, slots=True)
class TelegramIngressIdentity:
    """@brief Update 的规范入口身份 / Normalized ingress identity for an Update.

    @param update_id Telegram 的全局 Update 序号 / Telegram's global Update sequence.
    @param conversation_id 用于当前产品语义的长期会话身份 / Long-lived conversation identity for current product semantics.
    @note 私聊按用户聚合；群聊按 ``group_id + message_thread_id`` 聚合，使同一群 Topic
    的所有成员共享 Context。/ Private chats aggregate by user; group chats aggregate by
    ``group_id + message_thread_id`` so all members of one topic share Context.
    """

    update_id: UpdateId
    conversation_id: ConversationId


class TelegramUpdateMapper:
    """@brief 将 PTB Update 规范化为可持久化模型 / Normalize PTB Updates into persistable models.

    @note mapper 不决定业务命令，也不执行 handler；它只建立可靠入口所需的身份与
    JSON 边界。/ The mapper neither chooses business commands nor executes handlers; it
    only establishes identity and the JSON boundary required by durable ingress.
    """

    def map(self, update: Update, *, received_at: datetime) -> InboundUpdate:
        """@brief 创建待领取的 durable inbox 记录 / Create a claimable durable-inbox record.

        @param update PTB 收到的 Update / Update received by PTB.
        @param received_at adapter 观察到 Update 的时间 / Time at which the adapter observed the Update.
        @return 已校验的待处理入口实体 / Validated pending ingress entity.
        @raise ValueError Update 缺少合法 ID 或序列化结果不是 JSON object 时抛出 /
        Raised when the Update lacks a valid ID or serializes to a non-object JSON value.
        """

        identity = self.identity_for(update)
        return InboundUpdate.pending(
            update_id=identity.update_id,
            conversation_id=identity.conversation_id,
            payload=self._json_payload(update),
            received_at=received_at,
        )

    def identity_for(self, update: Update) -> TelegramIngressIdentity:
        """@brief 解析 Update 的幂等与会话身份 / Resolve idempotency and conversation identities.

        @param update 待解析 PTB Update / PTB Update to inspect.
        @return 规范入口身份 / Normalized ingress identity.
        @raise ValueError update_id 缺失或不是非负整数时抛出 /
        Raised when update_id is missing or is not a non-negative integer.
        """

        raw_update_id = update.update_id
        if isinstance(raw_update_id, bool) or not isinstance(raw_update_id, int):
            raise ValueError("Telegram Update requires an integer update_id")
        update_id = UpdateId(raw_update_id)

        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        message = getattr(update, "effective_message", None)
        if user is not None or chat is not None:
            conversation_id = TelegramConversationAddress(
                chat_type=None if chat is None else str(chat.type),
                chat_id=None if chat is None else chat.id,
                user_id=None if user is None else user.id,
                message_thread_id=(
                    None
                    if message is None
                    else getattr(message, "message_thread_id", None)
                ),
            ).conversation_id
        else:
            conversation_id = ConversationId(f"telegram-update:{raw_update_id}")
        return TelegramIngressIdentity(
            update_id=update_id,
            conversation_id=conversation_id,
        )

    @staticmethod
    def _json_payload(update: Update) -> JsonObject:
        """@brief 通过 PTB 公共 JSON 编码器建立持久化边界 / Establish the persistence boundary via PTB's public JSON encoder.

        @param update 待序列化 Update / Update to serialize.
        @return 仅含 JSON 值的 object / Object containing only JSON values.
        @raise ValueError 编码结果不是 JSON object 时抛出 / Raised when the encoded value is not a JSON object.
        @note 使用 ``to_json`` 而非依赖 SDK 内部属性；随后重新解析可确保 datetime、
        enum 与 TelegramObject 已经转换成数据库 JSONB 可接受的值。/
        ``to_json`` avoids SDK internals; parsing it back proves datetime, enums, and nested
        TelegramObjects have been converted into JSONB-compatible values.
        """

        decoded: JsonValue = cast(JsonValue, json.loads(update.to_json()))
        if not isinstance(decoded, dict):
            raise ValueError("Telegram Update JSON must be an object")
        return decoded


__all__ = ["TelegramIngressIdentity", "TelegramUpdateMapper"]
