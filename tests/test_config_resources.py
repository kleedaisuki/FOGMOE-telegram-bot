from fogmoe_bot.infrastructure import config


def test_text_resources_are_loaded_verbatim() -> None:
    """@brief 验证文本资源逐字加载 / Verify text resources are loaded verbatim."""

    assert config.HELP_TEXT == (
        config.BASE_DIR / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert config.SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")


def test_help_text_uses_telegram_legacy_markdown_delimiters() -> None:
    """@brief 验证帮助文本使用 Telegram 旧版 Markdown 标记 / Verify help text uses Telegram legacy Markdown delimiters."""

    assert "***" not in config.HELP_TEXT
    assert "*指令列表：*" in config.HELP_TEXT
    assert "*群组相关：*" in config.HELP_TEXT
    assert "*聊天相关：*" in config.HELP_TEXT
    assert "*数据相关：*" in config.HELP_TEXT
    assert "*娱乐相关：*" in config.HELP_TEXT


def test_system_prompt_defines_persona_and_runtime_contract() -> None:
    """@brief 验证 system prompt 身份与运行时契约 / Verify the system-prompt identity and runtime contract."""

    assert config.SYSTEM_PROMPT.startswith("# Runtime Contract\n\n## Persona\n")
    persona, separator, identity_contract = config.SYSTEM_PROMPT.partition(
        "\n## Identity and priority\n"
    )
    assert separator
    assert persona.removeprefix("# Runtime Contract\n\n## Persona\n").strip()
    assert identity_contract.strip()
    assert "# Runtime Contract\n" in config.SYSTEM_PROMPT
    assert config.SYSTEM_PROMPT.endswith("public repository when appropriate.\n")
    assert "@kleek_RoPL_bot" in config.SYSTEM_PROMPT
    assert "## Memory and User Profile\n" in config.SYSTEM_PROMPT
    assert "current explicit statement overrides" in config.SYSTEM_PROMPT
    assert "empty retrieval result does not prove" in config.SYSTEM_PROMPT
    assert "Call `search_memory`" in config.SYSTEM_PROMPT
    assert "Never attempt to cross personal or group boundaries" in config.SYSTEM_PROMPT
    assert "## Group conversations\n" in config.SYSTEM_PROMPT
    assert "Preserve speaker attribution" in config.SYSTEM_PROMPT
    assert "Private User Profile" in config.SYSTEM_PROMPT
    assert "Use `fetch_group_context`" in config.SYSTEM_PROMPT


def test_system_prompt_requires_the_typed_sticker_tool() -> None:
    """@brief 验证贴纸经 typed tool 发送 / Verify stickers are sent through the typed tool."""

    assert "first call `list_available_stickers`" in config.SYSTEM_PROMPT
    assert "then call `send_sticker`" in config.SYSTEM_PROMPT
    assert "[sticker_pack:" not in config.SYSTEM_PROMPT
    assert "file_id" in config.SYSTEM_PROMPT
