from fogmoe_bot.infrastructure import config


def test_text_resources_are_loaded_verbatim():
    assert config.HELP_TEXT == (
        config.BASE_DIR / "resources" / "telegram_help.md"
    ).read_text(encoding="utf-8")
    assert config.SYSTEM_PROMPT == (
        config.BASE_DIR / "resources" / "prompts" / "system_prompt.md"
    ).read_text(encoding="utf-8")


def test_system_prompt_defines_persona_and_runtime_contract():
    assert config.SYSTEM_PROMPT.startswith("# Runtime Contract\n\n## Persona\n")
    assert "Asuhoshi Yume" in config.SYSTEM_PROMPT
    assert "# Runtime Contract\n" in config.SYSTEM_PROMPT
    assert config.SYSTEM_PROMPT.endswith("public repository when appropriate.\n")
    assert "@kleek_RoPL_bot" in config.SYSTEM_PROMPT


def test_system_prompt_includes_sticker_directive_examples():
    """@brief 验证贴纸指令示例存在 / Verify sticker directive examples exist."""

    assert "[sticker_pack:<pack name> emoji:<emoji>]" in config.SYSTEM_PROMPT
    assert "[sticker_pack:WhiteWind emoji:😊]" in config.SYSTEM_PROMPT
    assert "[sticker_pack:DonutTheDog emoji:😢]" in config.SYSTEM_PROMPT
