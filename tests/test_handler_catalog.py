import re
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest
from observability_testkit import make_observability
from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_bot.application.banking.service import BANK_SERVICE_DATA_KEY, BankService
from fogmoe_bot.application.billing.service import (
    BILLING_SERVICE_DATA_KEY,
    BillingService,
)
from fogmoe_bot.application.chance.workflow import (
    CHANCE_WORKFLOW_DATA_KEY,
    ChanceWorkflow,
)
from fogmoe_bot.application.games.omikuji.service import (
    OMIKUJI_SERVICE_DATA_KEY,
    OmikujiService,
)
from fogmoe_bot.application.media.music_service import (
    MUSIC_SERVICE_DATA_KEY,
    MusicService,
)
from fogmoe_bot.application.media.picture_service import (
    PICTURE_SERVICE_DATA_KEY,
    PictureService,
)
from fogmoe_bot.application.moderation.verification_service import (
    VERIFICATION_SERVICE_DATA_KEY,
)
from fogmoe_bot.application.moderation.verification_worker import (
    VERIFICATION_WORKER_DATA_KEY,
)
from fogmoe_bot.application.personal_rpg.service import (
    PERSONAL_RPG_SERVICE_DATA_KEY,
    PersonalRpgService,
)
from fogmoe_bot.application.town.service import TOWN_SERVICE_DATA_KEY, TownService
from fogmoe_bot.config import BotSettings
from fogmoe_bot.presentation.telegram import handler_composition
from fogmoe_bot.presentation.telegram.handler_catalog import (
    HANDLER_CATALOG,
    DuplicateCallbackNamespaceError,
    DuplicateCommandError,
    DuplicateHandlerNameError,
    ErrorHandlerDefinition,
    HandlerCatalog,
    HandlerDefinition,
    HandlerKind,
    TelegramApplication,
    install_error_policy,
)
from fogmoe_bot.presentation.telegram.handler_composition import (
    assemble_handler_capabilities,
)
from fogmoe_bot.presentation.telegram.moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
)
from fogmoe_bot.resources import load_resources


class FakeApplication:
    def __init__(self) -> None:
        self.handlers: list[tuple[object, int]] = []
        self.error_handlers: list[object] = []
        self.bot_data: dict[str, object] = {}
        self.bot = object()

    def add_handler(self, handler: object, group: int = 0) -> None:
        self.handlers.append((handler, group))

    def add_error_handler(self, callback: object) -> None:
        self.error_handlers.append(callback)


async def _noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del update, context


def _settings() -> BotSettings:
    """@brief 构造 capability 装配需要的最小设置 / Build minimum settings for capability composition.

    @return 具有测试 Bot token 的设置 / Settings with a test Bot token.
    """

    return BotSettings.model_validate({"telegram": {"bot_token": "test-token"}})


def test_catalog_explicitly_declares_stable_primary_handlers() -> None:
    assert [definition.name for definition in HANDLER_CATALOG] == [
        "basic.start",
        "monitor.start",
        "monitor.stop",
        "economy.task",
        "economy.task-callback",
        "verification.command",
        "verification.new-member",
        "verification.callback",
        "verification.member-left",
        "membership.bot-status",
        "moderation.keyword",
        "moderation.spam",
        "moderation.spam-help",
        "game.omikuji",
        "game.omikuji-callback",
        "economy.referral",
        "economy.referral-callback",
        "economy.checkin",
        "moderation.report",
        "crypto.chart",
        "media.picture",
        "media.music",
        "media.music-callback",
        "admin.web-password",
    ]
    assert Counter(definition.kind for definition in HANDLER_CATALOG) == {
        HandlerKind.COMMAND: 15,
        HandlerKind.CALLBACK: 6,
        HandlerKind.MESSAGE: 2,
        HandlerKind.CHAT_MEMBER: 1,
    }
    assert [item.name for item in HANDLER_CATALOG.error_definitions] == [
        "errors.default"
    ]


def test_error_policy_installs_only_error_callbacks_and_no_ptb_feature_handlers() -> (
    None
):
    application = FakeApplication()

    install_error_policy(cast(TelegramApplication, application))

    assert application.handlers == []
    assert application.bot_data == {}
    assert application.error_handlers == [HANDLER_CATALOG.error_definitions[0].callback]


def test_callback_patterns_preserve_existing_callback_data_protocols() -> None:
    patterns: dict[str, str] = {}
    for definition in HANDLER_CATALOG:
        if not isinstance(definition.handler, CallbackQueryHandler):
            continue
        pattern = definition.handler.pattern
        if isinstance(pattern, str):
            rendered_pattern = pattern
        elif isinstance(pattern, re.Pattern):
            rendered_pattern = pattern.pattern
        else:
            raise AssertionError(f"Unexpected callback pattern: {pattern!r}")
        patterns[definition.filter_namespace] = rendered_pattern

    assert patterns == {
        "task": r"^task_",
        "verify": r"^verify:",
        "spam_help": r"^spam_help$",
        "omikuji": r"^(?:omikuji:|omikuji_)",
        "ref": r"^ref_",
        "music": r"^music_",
    }


def test_capability_assembly_is_named_and_rejects_duplicate_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = FakeApplication()
    verification_service = object()
    verification_worker = object()
    monkeypatch.setattr(
        handler_composition,
        "create_verification_runtime",
        lambda bot: (verification_service, verification_worker),
    )
    typed_application = cast(TelegramApplication, application)
    assemble_handler_capabilities(
        typed_application,
        telemetry=make_observability().telemetry,
        settings=_settings(),
        resources=load_resources(log_directory=tmp_path / "logs"),
    )

    assert application.bot_data[VERIFICATION_SERVICE_DATA_KEY] is verification_service
    assert application.bot_data[VERIFICATION_WORKER_DATA_KEY] is verification_worker
    assert {
        "economy.staking.service",
        "fogmoe.rps_service",
        "games.gamble.service",
        "games.sicbo.service",
        "games.runtime",
    }.isdisjoint(application.bot_data)
    for key, expected_type in (
        (BANK_SERVICE_DATA_KEY, BankService),
        (BILLING_SERVICE_DATA_KEY, BillingService),
        (CHANCE_WORKFLOW_DATA_KEY, ChanceWorkflow),
        (PERSONAL_RPG_SERVICE_DATA_KEY, PersonalRpgService),
        (TOWN_SERVICE_DATA_KEY, TownService),
        (OMIKUJI_SERVICE_DATA_KEY, OmikujiService),
        (PICTURE_SERVICE_DATA_KEY, PictureService),
        (MUSIC_SERVICE_DATA_KEY, MusicService),
    ):
        assert isinstance(application.bot_data[key], expected_type)
    assert isinstance(
        application.bot_data[MODERATION_CAPABILITY_DATA_KEY],
        TelegramModerationCapability,
    )
    with pytest.raises(RuntimeError, match="verification runtime"):
        assemble_handler_capabilities(
            typed_application,
            telemetry=make_observability().telemetry,
            settings=_settings(),
            resources=load_resources(log_directory=tmp_path / "logs"),
        )


def test_catalog_rejects_duplicate_commands() -> None:
    first = HandlerDefinition(
        "one",
        HandlerKind.COMMAND,
        "same",
        CommandHandler("same", _noop),
    )
    second = HandlerDefinition(
        "two",
        HandlerKind.COMMAND,
        "same",
        CommandHandler("same", _noop),
    )

    with pytest.raises(DuplicateCommandError, match="same"):
        HandlerCatalog((first, second))


def test_definition_rejects_command_namespace_drift() -> None:
    with pytest.raises(ValueError, match="namespace"):
        HandlerDefinition(
            "drifted",
            HandlerKind.COMMAND,
            "declared",
            CommandHandler("actual", _noop),
        )


def test_catalog_rejects_duplicate_callback_namespaces() -> None:
    first = HandlerDefinition(
        "one",
        HandlerKind.CALLBACK,
        "same",
        CallbackQueryHandler(_noop, pattern=r"^one:"),
    )
    second = HandlerDefinition(
        "two",
        HandlerKind.CALLBACK,
        "same",
        CallbackQueryHandler(_noop, pattern=r"^two:"),
    )

    with pytest.raises(DuplicateCallbackNamespaceError, match="same"):
        HandlerCatalog((first, second))


def test_catalog_rejects_duplicate_stable_names_across_error_group() -> None:
    definition = HandlerDefinition(
        "same",
        HandlerKind.COMMAND,
        "one",
        CommandHandler("one", _noop),
    )

    with pytest.raises(DuplicateHandlerNameError, match="same"):
        HandlerCatalog(
            (definition,),
            errors=(ErrorHandlerDefinition("same", _noop),),
        )


def test_catalog_and_definitions_are_immutable() -> None:
    definition = HANDLER_CATALOG.definitions[0]

    assert isinstance(HANDLER_CATALOG.definitions, tuple)
    with pytest.raises(FrozenInstanceError):
        setattr(definition, "name", "changed")
    with pytest.raises(FrozenInstanceError):
        setattr(HANDLER_CATALOG, "_definitions", ())
