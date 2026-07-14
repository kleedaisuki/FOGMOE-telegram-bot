"""@brief Agent 资产确认 Telegram callback 适配器 / Telegram callback adapter for Agent asset confirmations."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.asset_actions.callbacks import AssetActionCallbackData
from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCode,
    AssetActionDecisionCommand,
)
from fogmoe_bot.application.asset_actions.service import (
    ASSET_ACTION_CONFIRMATION_SERVICE_DATA_KEY,
    AssetActionConfirmationService,
)


async def asset_action_confirmation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 快速确认并执行 owner 绑定的资产确认 callback / Quickly acknowledge and execute an owner-bound asset-confirmation callback.

    @param update durable inbox 重建的 Telegram Update / Telegram Update reconstructed by the durable inbox.
    @param context PTB callback context，提供组合根 capability / PTB callback context providing composition-root capabilities.
    @return None / None.
    @note callback payload 只含短 confirmation reference；owner、私聊、权限、过期和终态均由
        confirmation store 在执行点重新验证。/ The callback payload contains only a short
        confirmation reference; owner, private chat, authorization, expiry, and terminal state
        are all revalidated by the confirmation store at execution time.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    callback = _decode_callback(query.data)
    if callback is None:
        await query.answer("确认请求无效或已失效。", show_alert=True)
        return
    chat = update.effective_chat
    actor = query.from_user
    update_id = update.update_id
    if (
        chat is None
        or actor is None
        or chat.type != "private"
        or isinstance(chat.id, bool)
        or chat.id <= 0
        or chat.id != actor.id
        or isinstance(actor.id, bool)
        or actor.id <= 0
        or isinstance(update_id, bool)
        or update_id < 0
    ):
        await query.answer("资产确认只能由所有者在私聊中操作。", show_alert=True)
        return

    # Telegram clients display a spinner until answerCallbackQuery.  The following
    # durable state transition may call the bank, so acknowledge before it begins.
    await query.answer("已收到确认请求；最终结果会以消息发送。")
    result = await _service(context).decide(
        AssetActionDecisionCommand(
            confirmation_id=callback.confirmation_id,
            decision=callback.decision,
            actor_user_id=actor.id,
            chat_id=chat.id,
            update_id=update_id,
            decided_at=datetime.now(UTC),
        )
    )
    if result.code is AssetActionDecisionCode.PROCESSING:
        # A separate recovery worker owns expired execution recovery.  Marking this
        # callback complete avoids retrying a live lease in the ordinary inbox.
        return


def _decode_callback(value: str) -> AssetActionCallbackData | None:
    """@brief 将不可信 callback 文本解码为短引用 / Decode untrusted callback text into a short reference.

    @param value Telegram 提供的 callback_data / Callback data supplied by Telegram.
    @return 类型化引用；语法非法为 ``None`` / Typed reference, or ``None`` for invalid syntax.
    """

    try:
        return AssetActionCallbackData.decode(value)
    except ValueError:
        return None


def _service(context: ContextTypes.DEFAULT_TYPE) -> AssetActionConfirmationService:
    """@brief 从 PTB runtime capability 读取确认服务 / Read the confirmation service from the PTB runtime capability.

    @param context PTB callback context / PTB callback context.
    @return 已配置的确认服务 / Configured confirmation service.
    @raise RuntimeError capability 缺失或类型不正确时抛出 / Raised for a missing or incorrectly typed capability.
    """

    service = context.application.bot_data.get(
        ASSET_ACTION_CONFIRMATION_SERVICE_DATA_KEY
    )
    if not isinstance(service, AssetActionConfirmationService):
        raise RuntimeError("Asset-action confirmation service is not configured")
    return service


__all__ = ["asset_action_confirmation_callback"]
