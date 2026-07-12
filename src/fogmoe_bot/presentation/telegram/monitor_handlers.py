"""@brief BTC 监控 Telegram 薄 handler / Thin Telegram handlers for BTC monitoring."""

from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.market_monitor import (
    BTC_MONITOR_DATA_KEY,
    BtcPatternMonitor,
    MonitorControlResult,
)

from .runtime_settings import telegram_runtime_settings


def _monitor(context: ContextTypes.DEFAULT_TYPE) -> BtcPatternMonitor:
    """@brief 从组合根读取监控 capability / Load the monitor capability from the composition root.

    @param context PTB callback context / PTB callback context.
    @return 已注入监控服务 / Injected monitor service.
    @raise RuntimeError capability 缺失时抛出 / Raised when the capability is missing.
    """

    monitor = context.application.bot_data.get(BTC_MONITOR_DATA_KEY)
    if not isinstance(monitor, BtcPatternMonitor):
        raise RuntimeError("BTC monitor capability is unavailable")
    return monitor


async def start_btc_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 管理员启用当前 chat 的 BTC 监控 / Let an administrator enable BTC monitoring for this chat.

    @param update Telegram command Update / Telegram command Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return
    if user.id != telegram_runtime_settings(context).administrator_id:
        await message.reply_text("您没有权限执行此操作")
        return
    result = _monitor(context).start(chat.id)
    text = (
        "BTCUSDT事件合约价格模式监控已启动"
        if result is MonitorControlResult.STARTED
        else "BTCUSDT事件合约价格模式监控已在运行"
    )
    await message.reply_text(text)


async def stop_btc_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 管理员停止 BTC 监控 / Let an administrator stop BTC monitoring.

    @param update Telegram command Update / Telegram command Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if user.id != telegram_runtime_settings(context).administrator_id:
        await message.reply_text("您没有权限执行此操作")
        return
    result = _monitor(context).stop()
    text = (
        "BTCUSDT事件合约价格模式监控已停止"
        if result is MonitorControlResult.STOPPED
        else "BTCUSDT事件合约价格模式监控未运行"
    )
    await message.reply_text(text)


__all__ = ["start_btc_monitor", "stop_btc_monitor"]
