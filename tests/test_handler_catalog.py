from collections import Counter
from dataclasses import FrozenInstanceError
import re
from typing import cast

import pytest
from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from fogmoe_bot.application.games.gamble.service import (
    GAMBLE_SERVICE_DATA_KEY,
    GambleService,
)
from fogmoe_bot.application.games.omikuji.service import (
    OMIKUJI_SERVICE_DATA_KEY,
    OmikujiService,
)
from fogmoe_bot.application.games.rpg.character_service import (
    RPG_CHARACTER_SERVICE_DATA_KEY,
    RpgCharacterService,
)
from fogmoe_bot.application.games.ports.rpg.equipment import (
    RPG_EQUIPMENT_OPERATIONS_DATA_KEY,
    RpgEquipmentOperations,
)
from fogmoe_bot.application.games.rpg.inventory_service import (
    RPG_INVENTORY_SERVICE_DATA_KEY,
    RpgInventoryService,
)
from fogmoe_bot.application.games.rps_service import RPS_SERVICE_DATA_KEY
from fogmoe_bot.application.games.sicbo.service import (
    SICBO_SERVICE_DATA_KEY,
    SicBoService,
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
from fogmoe_bot.presentation.telegram import handler_composition
from fogmoe_bot.presentation.telegram.moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
)
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


def test_catalog_explicitly_declares_stable_primary_handlers() -> None:
    assert [definition.name for definition in HANDLER_CATALOG] == [
        "basic.start",
        "monitor.start",
        "monitor.stop",
        "game.gamble",
        "game.gamble-callback",
        "economy.shop",
        "economy.shop-callback",
        "economy.task",
        "economy.task-callback",
        "verification.command",
        "verification.new-member",
        "verification.callback",
        "verification.member-left",
        "membership.bot-status",
        "economy.stake",
        "economy.stake-callback",
        "crypto.predict",
        "crypto.predict-callback",
        "crypto.swap",
        "moderation.keyword",
        "moderation.spam",
        "moderation.spam-help",
        "game.omikuji",
        "game.omikuji-callback",
        "game.rps",
        "game.rps-callback",
        "economy.charge",
        "economy.create-code",
        "economy.recharge",
        "economy.topup-request",
        "economy.topup-admin",
        "game.sicbo",
        "game.sicbo-callback",
        "economy.referral",
        "economy.referral-callback",
        "economy.checkin",
        "moderation.report",
        "crypto.chart",
        "media.picture",
        "media.picture-hd",
        "media.music",
        "media.music-callback",
        "game.rpg",
        "admin.web-password",
    ]
    assert Counter(definition.kind for definition in HANDLER_CATALOG) == {
        HandlerKind.COMMAND: 26,
        HandlerKind.CALLBACK: 15,
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
        "gamble": r"^(?:gamble:|gamble_)",
        "shop": r"^shop_",
        "task": r"^task_",
        "verify": r"^verify:",
        "stake": r"^stake_",
        "crypto": r"^crypto_",
        "spam_help": r"^spam_help$",
        "omikuji": r"^(?:omikuji:|omikuji_)",
        "rps": r"^rps:",
        "topup_req": r"^topup_req_",
        "topup_admin": r"^topup_admin_",
        "sicbo": r"^(?:sb:|sicbo_)",
        "ref": r"^ref_",
        "pic_hd": r"^pic_hd_",
        "music": r"^music_",
    }


def test_capability_assembly_is_named_and_rejects_duplicate_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FakeApplication()
    verification_service = object()
    verification_worker = object()
    rps_service = object()
    monkeypatch.setattr(
        handler_composition,
        "create_verification_runtime",
        lambda bot: (verification_service, verification_worker),
    )
    monkeypatch.setattr(
        handler_composition,
        "create_rps_service",
        lambda bot: rps_service,
    )
    typed_application = cast(TelegramApplication, application)
    assemble_handler_capabilities(typed_application)

    assert application.bot_data[VERIFICATION_SERVICE_DATA_KEY] is verification_service
    assert application.bot_data[VERIFICATION_WORKER_DATA_KEY] is verification_worker
    assert application.bot_data[RPS_SERVICE_DATA_KEY] is rps_service
    for key, expected_type in (
        (GAMBLE_SERVICE_DATA_KEY, GambleService),
        (SICBO_SERVICE_DATA_KEY, SicBoService),
        (OMIKUJI_SERVICE_DATA_KEY, OmikujiService),
        (RPG_CHARACTER_SERVICE_DATA_KEY, RpgCharacterService),
        (RPG_EQUIPMENT_OPERATIONS_DATA_KEY, RpgEquipmentOperations),
        (RPG_INVENTORY_SERVICE_DATA_KEY, RpgInventoryService),
        (PICTURE_SERVICE_DATA_KEY, PictureService),
        (MUSIC_SERVICE_DATA_KEY, MusicService),
    ):
        assert isinstance(application.bot_data[key], expected_type)
    assert isinstance(
        application.bot_data[MODERATION_CAPABILITY_DATA_KEY],
        TelegramModerationCapability,
    )
    with pytest.raises(RuntimeError, match="verification runtime"):
        assemble_handler_capabilities(typed_application)


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
