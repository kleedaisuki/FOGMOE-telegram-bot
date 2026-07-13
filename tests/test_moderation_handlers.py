from telegram import Bot
from telegram.ext import CallbackQueryHandler, CommandHandler

from fogmoe_bot.presentation.telegram.handler_catalog import HANDLER_CATALOG
from fogmoe_bot.presentation.telegram.moderation_composition import (
    TelegramModerationCapability,
    create_moderation_ingress_capability,
)
from fogmoe_bot.resources import PROJECT_ROOT


def test_catalog_preserves_enabled_commands_and_spam_help_namespace() -> None:
    """@brief 治理命令目录保留受支持命名空间 / The moderation catalog preserves supported namespaces.

    @return None / None.
    """

    commands = {
        definition.filter_namespace: definition.handler.callback
        for definition in HANDLER_CATALOG
        if isinstance(definition.handler, CommandHandler)
    }
    callbacks = {
        definition.filter_namespace: definition.handler.pattern
        for definition in HANDLER_CATALOG
        if isinstance(definition.handler, CallbackQueryHandler)
    }

    assert commands["spam"].__name__ == "toggle_spam_control"
    assert commands["keyword"].__name__ == "keyword_command"
    assert commands["report"].__name__ == "report_command"
    assert "sf" not in commands
    assert callbacks["spam_help"].pattern == r"^spam_help$"


def test_composition_exposes_typed_ingress_capability_without_global_state() -> None:
    """@brief 组合根显式注入词表资源 / The composition root injects the word-list resource explicitly.

    @return None / None.
    """

    capability = create_moderation_ingress_capability(
        Bot("123456:test-token"),
        wordlist_path=PROJECT_ROOT / "resources" / "spam_words.txt",
    )

    assert isinstance(capability, TelegramModerationCapability)
    assert capability.guard.name == "moderation-content-policy"
    assert capability.observer.name == "telegram-group-observers"
