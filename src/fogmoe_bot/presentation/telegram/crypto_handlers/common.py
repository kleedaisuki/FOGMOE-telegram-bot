"""@brief 群组图表 Telegram handler 的组合辅助 / Composition helpers for group-chart Telegram handlers."""

from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.chart_service import (
    CHART_SERVICE_DATA_KEY,
    ChartService,
)


def chart_service(context: ContextTypes.DEFAULT_TYPE) -> ChartService:
    """@brief 从 ``bot_data`` 读取已配置图表服务 / Load the configured chart service from ``bot_data``.

    @param context PTB 默认 callback context / PTB default callback context.
    @return 已装配图表服务 / Configured chart service.
    @raise RuntimeError capability 缺失或类型不符时抛出 / Raised when the capability is missing or has the wrong type.
    """

    value = context.application.bot_data.get(CHART_SERVICE_DATA_KEY)
    if not isinstance(value, ChartService):
        raise RuntimeError("Chart service is not configured")
    return value
