"""@brief Bot 静态资源加载测试 / Tests for Bot static-resource loading."""

from pathlib import Path

from fogmoe_bot.resources import BotResources, PROJECT_ROOT, load_resources


def _resources() -> tuple[Path, BotResources]:
    """@brief 加载测试所需的 Bot 资源 / Load Bot resources needed by tests.

    @return 项目根目录与资源集合 / Project root and resource bundle.
    """

    return PROJECT_ROOT, load_resources(log_directory=PROJECT_ROOT / "logs")


def test_text_resources_are_loaded_verbatim() -> None:
    """@brief 验证文本资源逐字加载 / Verify text resources are loaded verbatim."""

    project_root, resources = _resources()

    assert resources.help_text == (
        project_root / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert resources.system_prompt == (
        project_root / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")


def test_help_text_uses_telegram_legacy_markdown_delimiters() -> None:
    """@brief 验证帮助文本使用 Telegram 旧版 Markdown 标记 / Verify help text uses Telegram legacy Markdown delimiters."""

    _, resources = _resources()

    assert "***" not in resources.help_text
    assert "*指令列表：*" in resources.help_text
    assert "*群组相关：*" in resources.help_text
    assert "*聊天相关：*" in resources.help_text
    assert "*账户、银行与权益（仅私聊）：*" in resources.help_text
    assert "*个人冒险（仅私聊）：*" in resources.help_text
    assert "*群组小镇（仅群聊或超级群）：*" in resources.help_text
    assert "*可验证随机活动（私聊、群聊或超级群）：*" in resources.help_text
    assert "*其他娱乐与工具：*" in resources.help_text


def test_system_prompt_defines_persona_and_runtime_contract() -> None:
    """@brief 验证 system prompt 身份与运行时契约 / Verify the system-prompt identity and runtime contract."""

    _, resources = _resources()

    assert resources.system_prompt.startswith("## Core Identity\n")
    assert "## Personality Traits\n" in resources.system_prompt
    assert "# Tool Calling\n" in resources.system_prompt
    assert resources.system_prompt.endswith("public repository when appropriate.\n")
    assert "@kleek_RoPL_bot" in resources.system_prompt
    assert (
        '<user_identity trust="trusted_platform_metadata">' in resources.system_prompt
    )
    assert "`display_name`" in resources.system_prompt
    assert "otherwise use `username`" in resources.system_prompt
    assert "Do not use `user_id` as a form of address" in resources.system_prompt
    assert (
        "explicit preference for how to be addressed takes precedence"
        in resources.system_prompt
    )
    assert "current_user_id" in resources.system_prompt
    assert "## Memory and User Profile\n" in resources.system_prompt
    assert "current explicit statement overrides" in resources.system_prompt
    assert "empty retrieval result does not prove" in resources.system_prompt
    assert "Call `search_memory`" in resources.system_prompt
    assert (
        "Never attempt to cross personal or group boundaries" in resources.system_prompt
    )
    assert "## Group conversations\n" in resources.system_prompt
    assert "Preserve speaker attribution" in resources.system_prompt
    assert "Private User Profile" in resources.system_prompt
    assert "Use `fetch_group_context`" in resources.system_prompt
    assert 'parse_mode="Markdown"' in resources.system_prompt
    assert "not CommonMark" in resources.system_prompt
    assert "Formatting entities must never nest" in resources.system_prompt


def test_system_prompt_requires_the_typed_sticker_tool() -> None:
    """@brief 验证贴纸经 typed tool 发送 / Verify stickers are sent through the typed tool."""

    _, resources = _resources()

    assert "first call `list_available_stickers`" in resources.system_prompt
    assert "then call `send_sticker`" in resources.system_prompt
    assert "[sticker_pack:" not in resources.system_prompt
    assert "file_id" in resources.system_prompt
