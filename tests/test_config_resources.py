from fogmoe_bot.infrastructure import config


def test_text_resources_are_loaded_verbatim() -> None:
    """@brief 验证文本资源逐字加载 / Verify text resources are loaded verbatim."""

    assert config.HELP_TEXT == (
        config.BASE_DIR / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert config.SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")


def test_system_prompt_defines_persona_and_runtime_contract() -> None:
    """@brief 验证 system prompt 身份与运行时契约 / Verify the system-prompt identity and runtime contract."""

    assert config.SYSTEM_PROMPT.startswith("# Runtime Contract\n\n## Persona\n")
    assert "Asuhoshi Yume" in config.SYSTEM_PROMPT
    assert "# Runtime Contract\n" in config.SYSTEM_PROMPT
    assert config.SYSTEM_PROMPT.endswith("public repository when appropriate.\n")
    assert "@kleek_RoPL_bot" in config.SYSTEM_PROMPT


def test_system_prompt_requires_the_typed_sticker_tool() -> None:
    """@brief 验证贴纸经 typed tool 发送 / Verify stickers are sent through the typed tool."""

    assert "first call `list_available_stickers`" in config.SYSTEM_PROMPT
    assert "then call `send_sticker`" in config.SYSTEM_PROMPT
    assert "[sticker_pack:" not in config.SYSTEM_PROMPT
    assert "file_id" in config.SYSTEM_PROMPT
