"""@brief Telegram 原生命令菜单的声明与协调 / Telegram native command-menu declaration and reconciliation."""

from __future__ import annotations

from collections.abc import Sequence

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

DEFAULT_COMMAND_MENU: tuple[BotCommand, ...] = (
    BotCommand("start", "开始使用 FogMoe"),
    BotCommand("help", "查看完整指令与用法"),
    BotCommand("github", "查看开源项目"),
)
"""@brief 未命中更具体作用域时的最小菜单 / Minimal menu used outside more specific scopes."""

PRIVATE_COMMAND_MENU: tuple[BotCommand, ...] = (
    BotCommand("start", "开始使用 FogMoe"),
    BotCommand("help", "查看完整指令与用法"),
    BotCommand("me", "注册或查看个人信息"),
    BotCommand("clear", "开始一段新对话"),
    BotCommand("setmyinfo", "设置个性化信息"),
    BotCommand("resetmem", "清空个人长期记忆"),
    BotCommand("resetprofile", "清除 User Profile"),
    BotCommand("regen", "重新生成 User Profile"),
    BotCommand("tl", "中英互译"),
    BotCommand("bank", "查看金币账户"),
    BotCommand("billing", "查看权益与订阅"),
    BotCommand("chance", "参与可验证随机活动"),
    BotCommand("lottery", "领取每日免费奖励"),
    BotCommand("task", "查看可用任务"),
    BotCommand("checkin", "每日签到"),
    BotCommand("ref", "查看邀请信息"),
    BotCommand("adventure", "查看个人冒险"),
    BotCommand("music", "搜索音乐"),
    BotCommand("pic", "获取随机图片"),
    BotCommand("chart", "查看代币图表"),
    BotCommand("omikuji", "抽取御神签"),
    BotCommand("webpassword", "设置 Web 登录密码"),
    BotCommand("github", "查看开源项目"),
)
"""@brief 私聊按常见任务排序的命令菜单 / Private-chat menu ordered by common user tasks."""

GROUP_COMMAND_MENU: tuple[BotCommand, ...] = (
    BotCommand("fogmoebot", "在群聊中呼叫 FogMoe"),
    BotCommand("help", "查看完整指令与用法"),
    BotCommand("town", "查看或建设群组小镇"),
    BotCommand("chance", "参与可验证随机活动"),
    BotCommand("task", "查看可用任务"),
    BotCommand("checkin", "每日签到"),
    BotCommand("report", "举报垃圾消息"),
    BotCommand("verify", "管理新成员验证"),
    BotCommand("spam", "配置垃圾消息管制"),
    BotCommand("keyword", "配置关键词回复"),
    BotCommand("resetgroup", "清空当前群共享记忆"),
    BotCommand("chart", "查看代币图表"),
    BotCommand("music", "搜索音乐"),
    BotCommand("pic", "获取随机图片"),
    BotCommand("omikuji", "抽取御神签"),
    BotCommand("github", "查看开源项目"),
)
"""@brief 群聊按协作任务排序的命令菜单 / Group-chat menu ordered by collaborative tasks."""


def _validate_menu(name: str, commands: Sequence[BotCommand]) -> None:
    """@brief 在发起网络调用前校验菜单不变量 / Validate menu invariants before network calls.

    @param name 诊断用菜单名 / Menu name used for diagnostics.
    @param commands Telegram 命令序列 / Telegram command sequence.
    @return None / None.
    @raise ValueError 菜单为空、超过 Telegram 上限或含重复命令时抛出 /
        Raised when a menu is empty, exceeds Telegram limits, or contains duplicates.
    """

    if not commands:
        raise ValueError(f"Telegram {name} command menu cannot be empty")
    if len(commands) > 100:
        raise ValueError(f"Telegram {name} command menu cannot exceed 100 commands")
    names = tuple(command.command for command in commands)
    if len(names) != len(set(names)):
        raise ValueError(f"Telegram {name} command menu contains duplicate commands")


async def install_telegram_command_menu(bot: Bot) -> None:
    """@brief 幂等协调默认、私聊与群聊命令菜单 / Idempotently reconcile default, private, and group command menus.

    @param bot 已初始化的 Telegram Bot / Initialized Telegram Bot.
    @return None / None.
    @raise RuntimeError Telegram 未确认任一菜单写入时抛出 /
        Raised when Telegram does not acknowledge any menu write.
    @note 调用方应让 Telegram 网络异常进入现有 bootstrap 重试策略，避免进程在没有
        用户导航入口的半初始化状态继续运行。/ The caller should route Telegram network
        failures through the existing bootstrap retry policy instead of running half-initialized
        without user navigation.
    """

    menus = (
        ("default", DEFAULT_COMMAND_MENU, BotCommandScopeDefault()),
        ("private", PRIVATE_COMMAND_MENU, BotCommandScopeAllPrivateChats()),
        ("group", GROUP_COMMAND_MENU, BotCommandScopeAllGroupChats()),
    )
    for name, commands, scope in menus:
        _validate_menu(name, commands)
        acknowledged = await bot.set_my_commands(commands, scope=scope)
        if not acknowledged:
            raise RuntimeError(f"Telegram did not acknowledge the {name} command menu")
    if not await bot.set_chat_menu_button(menu_button=MenuButtonCommands()):
        raise RuntimeError("Telegram did not acknowledge the default command menu button")


__all__ = [
    "DEFAULT_COMMAND_MENU",
    "GROUP_COMMAND_MENU",
    "PRIVATE_COMMAND_MENU",
    "install_telegram_command_menu",
]
