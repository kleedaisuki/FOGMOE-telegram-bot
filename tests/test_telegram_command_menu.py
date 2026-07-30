"""@brief Telegram 原生命令菜单测试 / Tests for the Telegram native command menu."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from fogmoe_bot.presentation.telegram.command_menu import (
    DEFAULT_COMMAND_MENU,
    GROUP_COMMAND_MENU,
    PRIVATE_COMMAND_MENU,
    install_telegram_command_menu,
)


def _commands(menu: tuple[BotCommand, ...]) -> tuple[str, ...]:
    """@brief 提取测试菜单中的命令名 / Extract command names from a test menu.

    @param menu BotCommand 序列 / Sequence of BotCommand values.
    @return 保序命令名 / Ordered command names.
    """

    return tuple(command.command for command in menu)


def test_command_menus_follow_private_and_group_user_tasks() -> None:
    """@brief 菜单按聊天作用域暴露相关入口 / Menus expose entries relevant to each chat scope."""

    private = _commands(PRIVATE_COMMAND_MENU)
    group = _commands(GROUP_COMMAND_MENU)

    assert private[:4] == ("start", "help", "me", "clear")
    assert {"bank", "billing", "adventure", "resetmem"} <= set(private)
    assert {"fogmoebot", "town", "report", "verify", "resetgroup"} <= set(group)
    assert {"bank", "billing", "adventure", "resetmem"}.isdisjoint(group)
    assert _commands(DEFAULT_COMMAND_MENU) == ("start", "help", "github")
    for menu in (DEFAULT_COMMAND_MENU, PRIVATE_COMMAND_MENU, GROUP_COMMAND_MENU):
        names = _commands(menu)
        assert len(names) == len(set(names))
        assert len(names) <= 100


def test_installer_reconciles_scoped_commands_and_private_menu_button() -> None:
    """@brief 安装器协调三个作用域和默认命令按钮 / Installer reconciles three scopes and the command button."""

    set_commands = AsyncMock(return_value=True)
    """@brief 记录作用域菜单写入 / Record scoped menu writes."""
    set_menu_button = AsyncMock(return_value=True)
    """@brief 记录默认菜单按钮写入 / Record the default menu-button write."""
    bot = cast(
        Bot,
        SimpleNamespace(
            set_my_commands=set_commands,
            set_chat_menu_button=set_menu_button,
        ),
    )

    asyncio.run(install_telegram_command_menu(bot))

    calls = set_commands.await_args_list
    assert [call.args[0] for call in calls] == [
        DEFAULT_COMMAND_MENU,
        PRIVATE_COMMAND_MENU,
        GROUP_COMMAND_MENU,
    ]
    assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)
    assert isinstance(calls[1].kwargs["scope"], BotCommandScopeAllPrivateChats)
    assert isinstance(calls[2].kwargs["scope"], BotCommandScopeAllGroupChats)
    menu_call = set_menu_button.await_args
    assert menu_call is not None
    button = menu_call.kwargs["menu_button"]
    assert isinstance(button, MenuButtonCommands)


def test_installer_rejects_unacknowledged_menu_write() -> None:
    """@brief Telegram 未确认写入时启动失败 / Startup fails when Telegram does not acknowledge a write."""

    set_menu_button = AsyncMock(return_value=True)
    """@brief 记录失败路径没有触发菜单按钮写入 / Record no menu-button write on failure."""
    bot = cast(
        Bot,
        SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=[True, False]),
            set_chat_menu_button=set_menu_button,
        ),
    )

    with pytest.raises(RuntimeError, match="private command menu"):
        asyncio.run(install_telegram_command_menu(bot))

    set_menu_button.assert_not_awaited()
