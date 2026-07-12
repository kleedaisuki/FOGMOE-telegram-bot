"""@brief Telegram 管理员展示名解析测试 / Tests for Telegram administrator display-name resolution."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from telegram import Bot

from fogmoe_bot.presentation.telegram.runtime_settings import (
    TelegramRuntimeSettings,
    resolve_administrator_contact_name,
)


def test_resolve_administrator_contact_prefers_public_username() -> None:
    """@brief API username 优先于全名与配置回退 / Prefer API username over full name and configured fallback."""

    bot = cast(Bot, SimpleNamespace(get_chat=AsyncMock()))
    bot.get_chat.return_value = SimpleNamespace(
        username="FogMoeAdmin",
        full_name="雾萌管理",
    )
    settings = TelegramRuntimeSettings(42, 10, "配置管理员")

    resolved = asyncio.run(resolve_administrator_contact_name(bot, settings))

    assert resolved.administrator_contact_label == "@FogMoeAdmin"
    bot.get_chat.assert_awaited_once_with(42)


def test_resolve_administrator_contact_uses_full_name_without_username() -> None:
    """@brief 无 username 时使用 Telegram 全名 / Use Telegram full name when no username exists."""

    bot = cast(Bot, SimpleNamespace(get_chat=AsyncMock()))
    bot.get_chat.return_value = SimpleNamespace(
        username=None,
        full_name="雾萌管理员",
    )
    settings = TelegramRuntimeSettings(42, 10, "配置管理员")

    resolved = asyncio.run(resolve_administrator_contact_name(bot, settings))

    assert resolved.administrator_contact_label == "雾萌管理员"


def test_administrator_contact_uses_generic_label_without_api_or_config() -> None:
    """@brief 无 API/配置名称时不暴露伪造身份 / Avoid inventing an identity without API or configuration."""

    settings = TelegramRuntimeSettings(42, 10)

    assert settings.administrator_contact_label == "管理员"


def test_resolve_administrator_contact_retains_configured_fallback() -> None:
    """@brief API 未提供名称时保留配置回退 / Retain the configured fallback when the API provides no name."""

    bot = cast(Bot, SimpleNamespace(get_chat=AsyncMock()))
    bot.get_chat.return_value = SimpleNamespace(username=None, full_name=None)
    settings = TelegramRuntimeSettings(42, 10, "配置管理员")

    resolved = asyncio.run(resolve_administrator_contact_name(bot, settings))

    assert resolved.administrator_contact_label == "配置管理员"
