"""@brief Durable router 到 PTB primary catalog 的正式边界 / Canonical boundary from the durable router to the PTB primary catalog.

本模块不调用 PTB 隐式 group dispatcher；它只执行 catalog 中首个互斥 primary callback。
守卫与观察者已经是 application router 的类型化阶段。/ This module does not invoke PTB's
implicit group dispatcher; it executes only the first mutually exclusive primary callback in the
catalog. Guards and observers are typed stages owned by the application router.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.conversation.inbox_worker import PermanentIngressError
from fogmoe_bot.application.conversation.router import (
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.runtime import WorkPriority
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .handler_catalog import HandlerCatalog, HandlerDefinition, TelegramApplication


class UpdatePredicate(Protocol):
    """@brief 纯 Update 所有权谓词 / Pure Update-ownership predicate."""

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 判断 route 是否拥有 Update / Decide whether the route owns the Update.

        @param update durable Update / Durable Update.
        @return 拥有时为 True / True when owned.
        """

        ...


class MalformedPersistedTelegramUpdate(PermanentIngressError):
    """@brief 无法安全重建的持久化 Update / Persisted Update that cannot be reconstructed safely."""


class TelegramCatalogDispatcher:
    """@brief 执行首个匹配的 PTB primary callback / Execute the first matching PTB primary callback."""

    def __init__(
        self,
        *,
        application: TelegramApplication,
        catalog: HandlerCatalog,
    ) -> None:
        """@brief 创建 catalog dispatcher / Create the catalog dispatcher.

        @param application 提供 Bot、Context 与 error policy 的 PTB Application /
            PTB Application providing the Bot, Context, and error policy.
        @param catalog 不可变 primary 定义 / Immutable primary definitions.
        """

        self._application = application
        self._catalog = catalog

    @property
    def application(self) -> TelegramApplication:
        """@brief 返回 capability 容器 / Return the capability container.

        @return PTB Application / PTB Application.
        """

        return self._application

    def first_match(
        self,
        update: Update,
    ) -> tuple[HandlerDefinition, object] | None:
        """@brief 按声明顺序解析首个匹配 / Resolve the first match in declaration order.

        @param update SDK Update / SDK Update.
        @return ``(definition, check_result)`` 或 None / ``(definition, check_result)`` or None.
        """

        for definition in self._catalog:
            check_result = definition.handler.check_update(update)
            if check_result is None or check_result is False:
                continue
            return definition, check_result
        return None

    async def dispatch(self, update: Update) -> str | None:
        """@brief 执行首个匹配 callback / Execute the first matching callback.

        @param update SDK Update / SDK Update.
        @return 稳定 handler 名；无匹配为 None / Stable handler name, or None.
        @note callback 异常委托 error policy 后继续抛出，使 durable inbox 重试。/
            Callback failures are delegated to the error policy and re-raised so the durable inbox
            retries.
        """

        matched = self.first_match(update)
        if matched is None:
            return None
        definition, check_result = matched
        context = ContextTypes.DEFAULT_TYPE.from_update(update, self._application)
        await context.refresh_data()
        try:
            await definition.handler.handle_update(
                update,
                self._application,
                check_result,
                context,
            )
        except Exception as error:
            await self._application.process_error(update=update, error=error)
            raise
        return definition.name


class TelegramCatalogPrimaryRoute:
    """@brief 将专用 route 的补集交给 PTB primary catalog / Dispatch the complement of dedicated routes to the PTB primary catalog."""

    def __init__(
        self,
        *,
        dispatcher: TelegramCatalogDispatcher,
        excluded: UpdatePredicate,
        additional_excluded: Sequence[UpdatePredicate] = (),
    ) -> None:
        """@brief 创建互斥 primary route / Create the mutually exclusive primary route.

        @param dispatcher PTB catalog dispatcher / PTB catalog dispatcher.
        @param excluded Assistant route / Assistant route.
        @param additional_excluded 其他专用 durable routes / Other dedicated durable routes.
        """

        self._dispatcher = dispatcher
        self._excluded = (excluded, *additional_excluded)

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名 / Return the stable route name.

        @return ``telegram-handler-catalog`` / ``telegram-handler-catalog``.
        """

        return "telegram-handler-catalog"

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 匹配专用 route 补集中的 catalog primary / Match a catalog primary outside dedicated routes.

        @param update durable Update / Durable Update.
        @return 存在互斥 primary 时为 True / True when a mutually exclusive primary exists.
        """

        if any(route.matches(update) for route in self._excluded):
            return False
        telegram_update = _reconstruct(update, self._dispatcher.application)
        return self._dispatcher.first_match(telegram_update) is not None

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造按 conversation 串行的 dispatch / Build a dispatch serialized by conversation.

        @param update 已匹配的 durable Update / Matched durable Update.
        @return keyed runtime 操作 / Keyed-runtime operation.
        """

        if any(route.matches(update) for route in self._excluded):
            raise MalformedPersistedTelegramUpdate(
                "Telegram handler route cannot process a dedicated-route Update"
            )
        telegram_update = _reconstruct(update, self._dispatcher.application)
        if self._dispatcher.first_match(telegram_update) is None:
            raise MalformedPersistedTelegramUpdate(
                "Telegram handler operation requires a matching primary"
            )

        async def call() -> None:
            """@brief 执行已验证的 primary / Execute the validated primary.

            @return None / None.
            """

            await self._dispatcher.dispatch(telegram_update)

        return RoutedOperation(
            name=f"telegram-handler-catalog:{update.update_id.value}",
            key=conversation_aggregate_key(update.conversation_id),
            call=call,
            priority=WorkPriority.NORMAL,
        )


def _reconstruct(
    update: InboundUpdate,
    application: TelegramApplication,
) -> Update:
    """@brief 重建并校验 Telegram SDK Update / Reconstruct and validate a Telegram SDK Update.

    @param update durable Update / Durable Update.
    @param application 提供反序列化 Bot / Application providing the deserialization Bot.
    @return 已绑定 Bot 的 SDK Update / SDK Update bound to the Bot.
    @raise MalformedPersistedTelegramUpdate payload 或 identity 非法 / Invalid payload or identity.
    """

    try:
        telegram_update = Update.de_json(
            cast(dict[str, Any], dict(update.payload)),
            application.bot,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedPersistedTelegramUpdate(
            f"Invalid persisted Telegram Update {update.update_id.value}: {error}"
        ) from error
    if telegram_update.update_id != update.update_id.value:
        raise MalformedPersistedTelegramUpdate(
            "Reconstructed Telegram update_id differs from durable identity"
        )
    return telegram_update


__all__ = [
    "MalformedPersistedTelegramUpdate",
    "TelegramCatalogDispatcher",
    "TelegramCatalogPrimaryRoute",
    "UpdatePredicate",
]
