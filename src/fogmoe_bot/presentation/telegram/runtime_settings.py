"""@brief Telegram 适配器的不可变运行配置 / Immutable runtime settings for Telegram adapters."""

from dataclasses import dataclass

from telegram.ext import ContextTypes


TELEGRAM_SETTINGS_DATA_KEY = "fogmoe.telegram.settings"
"""@brief ``bot_data`` 中 Telegram 配置的稳定键 / Stable Telegram-settings key in ``bot_data``."""


@dataclass(frozen=True, slots=True)
class TelegramRuntimeSettings:
    """@brief 由组合根注入 handler 的配置 / Settings injected into handlers by the composition root.

    @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
    @param new_user_bonus 新用户初始金币 / Initial coins granted to a new user.
    """

    administrator_id: int
    new_user_bonus: int

    def __post_init__(self) -> None:
        """@brief 校验配置边界 / Validate setting bounds.

        @return None / None.
        @raise ValueError 用户 ID 或奖励非法时抛出 / Raised for an invalid user ID or bonus.
        """

        if self.administrator_id <= 0:
            raise ValueError("administrator_id must be positive")
        if self.new_user_bonus < 0:
            raise ValueError("new_user_bonus cannot be negative")


def telegram_runtime_settings(
    context: ContextTypes.DEFAULT_TYPE,
) -> TelegramRuntimeSettings:
    """@brief 读取已注入的 Telegram 配置 / Read the injected Telegram settings.

    @param context PTB callback context / PTB callback context.
    @return 已校验的不可变配置 / Validated immutable settings.
    @raise RuntimeError 组合根未注入配置时抛出 / Raised when composition did not inject settings.
    """

    value = context.application.bot_data.get(TELEGRAM_SETTINGS_DATA_KEY)
    if not isinstance(value, TelegramRuntimeSettings):
        raise RuntimeError("Telegram runtime settings are unavailable")
    return value


__all__ = [
    "TELEGRAM_SETTINGS_DATA_KEY",
    "TelegramRuntimeSettings",
    "telegram_runtime_settings",
]
