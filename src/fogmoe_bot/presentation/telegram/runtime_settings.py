"""@brief Telegram 适配器的不可变运行配置 / Immutable runtime settings for Telegram adapters."""

from dataclasses import dataclass, replace

from telegram import Bot
from telegram.ext import ContextTypes


TELEGRAM_SETTINGS_DATA_KEY = "fogmoe.telegram.settings"
"""@brief ``bot_data`` 中 Telegram 配置的稳定键 / Stable Telegram-settings key in ``bot_data``."""


@dataclass(frozen=True, slots=True)
class TelegramRuntimeSettings:
    """@brief 由组合根注入 handler 的配置 / Settings injected into handlers by the composition root.

    @param administrator_id 管理员 Telegram 用户 ID / Administrator Telegram user ID.
    @param new_user_bonus 新用户初始金币 / Initial coins granted to a new user.
    @param administrator_contact_name 管理员展示名 / Administrator display name.
    """

    administrator_id: int
    new_user_bonus: int
    administrator_contact_name: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验配置边界 / Validate setting bounds.

        @return None / None.
        @raise ValueError 用户 ID 或奖励非法时抛出 / Raised for an invalid user ID or bonus.
        """

        if self.administrator_id <= 0:
            raise ValueError("administrator_id must be positive")
        if (
            self.administrator_contact_name is not None
            and not self.administrator_contact_name.strip()
        ):
            raise ValueError("administrator_contact_name cannot be blank")
        if self.new_user_bonus < 0:
            raise ValueError("new_user_bonus cannot be negative")

    @property
    def administrator_contact_label(self) -> str:
        """@brief 返回可安全展示的管理员名称 / Return a safe administrator label.

        @return API 或配置得到的名称；不可用时返回通用称谓 / API/configured name, or a generic label.
        """

        return self.administrator_contact_name or "管理员"


async def resolve_administrator_contact_name(
    bot: Bot,
    settings: TelegramRuntimeSettings,
) -> TelegramRuntimeSettings:
    """@brief 通过 Telegram API 解析管理员展示名 / Resolve the administrator display name via the Telegram API.

    @param bot 已初始化的 Telegram Bot / Initialized Telegram Bot.
    @param settings 当前运行配置 / Current runtime settings.
    @return 使用 API 名称更新后的不可变配置 / Immutable settings updated with the API name.
    @note 优先使用公开 username；无 username 时回退到 Telegram 全名，二者均不存在则保留配置回退值 /
        Prefer a public username, then Telegram full name; retain the configured fallback when neither exists.
    """

    chat = await bot.get_chat(settings.administrator_id)
    if chat.username:
        return replace(settings, administrator_contact_name=f"@{chat.username}")
    if chat.full_name:
        return replace(settings, administrator_contact_name=chat.full_name)
    return settings


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
    "resolve_administrator_contact_name",
    "telegram_runtime_settings",
]
