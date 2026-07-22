"""@brief Durable Telegram primary-catalog 边界测试 / Tests for the durable Telegram primary-catalog boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from telegram import Update, User
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.catalog_route import (
    MalformedPersistedTelegramUpdate,
    TelegramCatalogDispatcher,
    TelegramCatalogPrimaryRoute,
)
from fogmoe_bot.presentation.telegram.handler_catalog import (
    HandlerCatalog,
    HandlerDefinition,
    HandlerKind,
    TelegramApplication,
)

type TelegramContext = ContextTypes.DEFAULT_TYPE
"""@brief 测试 callback context / Test callback context."""


class _Predicate:
    """@brief 固定 Update 所有权谓词 / Fixed Update-ownership predicate."""

    def __init__(self, matches: bool) -> None:
        """@brief 保存固定结果 / Store the fixed result.

        @param matches 固定结果 / Fixed result.
        """

        self._matches = matches

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 返回固定结果 / Return the fixed result.

        @param update 未使用 Update / Unused Update.
        @return 固定结果 / Fixed result.
        """

        del update
        return self._matches


def _application() -> TelegramApplication:
    """@brief 创建离线 PTB Application / Build an offline PTB Application.

    @return 测试 Application / Test Application.
    """

    application = ApplicationBuilder().token("123456:ABCDEF_test_token").build()
    object.__setattr__(
        application.bot,
        "_bot_user",
        User(id=999, first_name="Fog", is_bot=True, username="FogMoeBot"),
    )
    return application


def _inbound(*, text: str = "/help", payload_update_id: int = 7) -> InboundUpdate:
    """@brief 构造持久化 Telegram Update / Build a persisted Telegram Update.

    @param text 消息文本 / Message text.
    @param payload_update_id payload identity / Payload identity.
    @return durable Update / Durable Update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(7),
        conversation_id=ConversationId("assistant-user:42"),
        payload={
            "update_id": payload_update_id,
            "message": {
                "message_id": 9,
                "date": 1_893_456_000,
                "chat": {"id": 42, "type": "private"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Klee",
                },
                "text": text,
                "entities": (
                    [{"type": "bot_command", "offset": 0, "length": len(text)}]
                    if text.startswith("/")
                    else []
                ),
            },
        },
        received_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _catalog(events: list[str]) -> HandlerCatalog:
    """@brief 创建单 primary catalog / Build a one-primary catalog.

    @param events callback 记录 / Callback records.
    @return 测试 catalog / Test catalog.
    """

    async def primary(update: Update, context: TelegramContext) -> None:
        """@brief 记录 primary 调用 / Record a primary call.

        @param update Update / Update.
        @param context callback context / Callback context.
        @return None / None.
        """

        del update, context
        events.append("primary")

    return HandlerCatalog(
        (
            HandlerDefinition(
                name="test.help",
                kind=HandlerKind.COMMAND,
                filter_namespace="help",
                handler=CommandHandler("help", primary),
            ),
        )
    )


def test_primary_route_dispatches_once_with_conversation_key() -> None:
    """@brief primary route 只调一次并使用 conversation key / The primary route dispatches once with a conversation key."""

    async def scenario() -> None:
        """@brief 执行 route / Execute the route.

        @return None / None.
        """

        events: list[str] = []
        route = TelegramCatalogPrimaryRoute(
            dispatcher=TelegramCatalogDispatcher(
                application=_application(),
                catalog=_catalog(events),
            ),
            excluded=_Predicate(False),
        )
        inbound = _inbound()
        assert route.matches(inbound)
        operation = await route.operation(inbound)
        assert operation.key.aggregate_type == "conversation"
        assert operation.key.identity == ("assistant-user:42",)
        await operation.call()
        assert events == ["primary"]

    asyncio.run(scenario())


def test_dedicated_route_ownership_excludes_catalog() -> None:
    """@brief 专用 route 所有权排除 catalog / Dedicated-route ownership excludes the catalog."""

    route = TelegramCatalogPrimaryRoute(
        dispatcher=TelegramCatalogDispatcher(
            application=_application(),
            catalog=_catalog([]),
        ),
        excluded=_Predicate(True),
    )
    assert not route.matches(_inbound())


def test_unmatched_update_is_not_claimed() -> None:
    """@brief 无 primary 的消息不由 catalog 占有 / An update without a primary is not owned by the catalog."""

    route = TelegramCatalogPrimaryRoute(
        dispatcher=TelegramCatalogDispatcher(
            application=_application(),
            catalog=_catalog([]),
        ),
        excluded=_Predicate(False),
    )
    assert not route.matches(_inbound(text="ordinary text"))


def test_identity_drift_is_permanently_rejected() -> None:
    """@brief payload identity 漂移被永久隔离 / Payload identity drift is quarantined permanently."""

    route = TelegramCatalogPrimaryRoute(
        dispatcher=TelegramCatalogDispatcher(
            application=_application(),
            catalog=_catalog([]),
        ),
        excluded=_Predicate(False),
    )
    with pytest.raises(MalformedPersistedTelegramUpdate):
        route.matches(_inbound(payload_update_id=8))


def test_dispatcher_reports_callback_error_and_rethrows_for_inbox_retry() -> None:
    """@brief callback 异常经过 error policy 后继续抛出 / Callback failures reach the error policy and are re-raised."""

    async def scenario() -> None:
        """@brief 执行失败 callback / Execute a failing callback.

        @return None / None.
        """

        async def fail(update: Update, context: TelegramContext) -> None:
            """@brief 抛出测试异常 / Raise the test error.

            @param update Update / Update.
            @param context callback context / Callback context.
            @return None / None.
            """

            del update, context
            raise RuntimeError("boom")

        application = _application()
        seen: list[str] = []

        async def process_error(update: object, context: TelegramContext) -> None:
            """@brief 记录 error policy / Record the error policy.

            @param update 失败 Update / Failed Update.
            @param context error context / Error context.
            @return None / None.
            """

            del update
            seen.append(str(context.error))

        application.add_error_handler(process_error)
        dispatcher = TelegramCatalogDispatcher(
            application=application,
            catalog=HandlerCatalog(
                (
                    HandlerDefinition(
                        name="test.fail",
                        kind=HandlerKind.COMMAND,
                        filter_namespace="help",
                        handler=CommandHandler("help", fail),
                    ),
                )
            ),
        )
        telegram_update = Update.de_json(
            cast(dict[str, Any], dict(_inbound().payload)),
            application.bot,
        )
        with pytest.raises(RuntimeError, match="boom"):
            await dispatcher.dispatch(telegram_update)
        assert seen == ["boom"]

    asyncio.run(scenario())
