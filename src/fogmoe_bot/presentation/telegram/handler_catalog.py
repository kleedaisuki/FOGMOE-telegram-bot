"""@brief Telegram handler 的单一不可变目录 / Single immutable Telegram handler catalog.

本模块显式声明每个 Command、Callback、Message 与 ChatMember handler 的稳定名称、
过滤命名空间和 PTB handler。构造时拒绝重复命令与 callback 命名空间；服务装配与
handler 声明保持为两个具名阶段。
/ This module explicitly declares the stable name, filter namespace, PTB handler,
for every Command, Callback, Message, and ChatMember handler. Duplicate commands
and callback namespaces are rejected before dispatch; capability assembly, handler
dispatch, and PTB error-policy installation remain separate named phases.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, cast

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import (
    economy_handlers,
    moderation_handlers,
    verification_handlers,
)
from .crypto_handlers import chart as crypto_chart
from .game_handlers import omikuji
from .media_handlers import music as media_music
from .media_handlers import picture as media_picture
from .monitor_handlers import start_btc_monitor, stop_btc_monitor
from .error_policy import telegram_error_handler
from .membership_handler import bot_membership_changed


type TelegramApplication = Application[Any, Any, Any, Any, Any, Any]
"""@brief 使用 PTB 默认泛型配置的 Application / Application using PTB's default generic configuration."""

type TelegramContext = ContextTypes.DEFAULT_TYPE
"""@brief PTB 默认 callback context / PTB default callback context."""

type TelegramHandler = (
    CommandHandler[TelegramContext, None]
    | CallbackQueryHandler[TelegramContext, None]
    | MessageHandler[TelegramContext, None]
    | ChatMemberHandler[TelegramContext, None]
)
"""@brief catalog 支持的 PTB handler 穷尽联合 / Exhaustive PTB-handler union supported by the catalog."""

type HandlerCallback = Callable[[Update, TelegramContext], Coroutine[Any, Any, None]]
"""@brief Telegram update callback / Telegram update callback."""

type ErrorCallback = HandlerCallback
"""@brief Telegram error callback / Telegram error callback."""


class HandlerKind(StrEnum):
    """@brief Telegram handler 类别 / Telegram handler kind."""

    COMMAND = "command"
    """@brief 命令 handler / Command handler."""

    CALLBACK = "callback"
    """@brief callback-query handler / Callback-query handler."""

    MESSAGE = "message"
    """@brief 消息 handler / Message handler."""

    CHAT_MEMBER = "chat_member"
    """@brief 成员状态 handler / Chat-member handler."""


@dataclass(frozen=True, slots=True)
class HandlerDefinition:
    """@brief 一个不可变 Telegram handler 定义 / One immutable Telegram handler definition.

    @param name catalog 内稳定唯一名称 / Stable unique catalog name.
    @param kind handler 类别 / Handler kind.
    @param filter_namespace 互斥过滤命名空间 / Disjoint filter namespace.
    @param handler 已构造的 PTB handler / Constructed PTB handler.
    """

    name: str
    kind: HandlerKind
    filter_namespace: str
    handler: TelegramHandler

    def __post_init__(self) -> None:
        """@brief 校验单个定义内部一致性 / Validate one definition's internal consistency.

        @return None / None.
        @raise ValueError 名称、命名空间或类型不一致时抛出 / Raised for an inconsistent name, namespace, or kind.
        """

        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", self.name) is None:
            raise ValueError(f"Invalid handler name: {self.name!r}")
        if not self.filter_namespace.strip():
            raise ValueError(f"Handler {self.name!r} has an empty filter namespace")
        expected_type: type[object]
        if self.kind is HandlerKind.COMMAND:
            expected_type = CommandHandler
        elif self.kind is HandlerKind.CALLBACK:
            expected_type = CallbackQueryHandler
        elif self.kind is HandlerKind.MESSAGE:
            expected_type = MessageHandler
        else:
            expected_type = ChatMemberHandler
        if not isinstance(self.handler, expected_type):
            raise ValueError(
                f"Handler {self.name!r} kind {self.kind.value!r} does not match "
                f"{type(self.handler).__name__}"
            )
        if self.kind is HandlerKind.COMMAND:
            command_handler = self.handler
            if not isinstance(command_handler, CommandHandler):
                raise AssertionError("Validated command handler lost its type")
            if command_handler.commands != frozenset((self.filter_namespace,)):
                raise ValueError(
                    f"Command handler {self.name!r} namespace does not match its command"
                )


@dataclass(frozen=True, slots=True)
class ErrorHandlerDefinition:
    """@brief 显式 error group 定义 / Explicit error-group definition.

    @param name catalog 内稳定唯一名称 / Stable unique catalog name.
    @param callback PTB error callback / PTB error callback.
    """

    name: str
    callback: ErrorCallback


class DuplicateHandlerNameError(ValueError):
    """@brief catalog handler 名称重复 / Duplicate catalog handler name."""


class DuplicateCommandError(ValueError):
    """@brief Telegram 命令重复 / Duplicate Telegram command."""


class DuplicateCallbackNamespaceError(ValueError):
    """@brief callback filter 命名空间重复 / Duplicate callback-filter namespace."""


@dataclass(frozen=True, slots=True, init=False)
class HandlerCatalog:
    """@brief 保序且不可变的 Telegram handler 目录 / Ordered immutable Telegram handler catalog."""

    _definitions: tuple[HandlerDefinition, ...]
    _error_definitions: tuple[ErrorHandlerDefinition, ...]

    def __init__(
        self,
        definitions: Sequence[HandlerDefinition],
        *,
        errors: Sequence[ErrorHandlerDefinition] = (),
    ) -> None:
        """@brief 创建并完整校验目录 / Create and fully validate a catalog.

        @param definitions Telegram handler 定义 / Telegram handler definitions.
        @param errors error group 定义 / Error-group definitions.
        @raise DuplicateHandlerNameError 稳定名称重复时抛出 / Raised for a duplicate stable name.
        @raise DuplicateCommandError 命令重复时抛出 / Raised for a duplicate command.
        @raise DuplicateCallbackNamespaceError callback 命名空间重复时抛出 / Raised for a duplicate callback namespace.
        """

        ordered = tuple(definitions)
        error_definitions = tuple(errors)
        names: set[str] = set()
        commands: set[str] = set()
        callback_namespaces: set[str] = set()
        for definition in ordered:
            if definition.name in names:
                raise DuplicateHandlerNameError(
                    f"Duplicate handler name: {definition.name}"
                )
            names.add(definition.name)
            if definition.kind is HandlerKind.COMMAND:
                command_handler = definition.handler
                if not isinstance(command_handler, CommandHandler):
                    raise AssertionError("Validated command definition lost its type")
                for command in command_handler.commands:
                    if command in commands:
                        raise DuplicateCommandError(f"Duplicate command: {command}")
                    commands.add(command)
            elif definition.kind is HandlerKind.CALLBACK:
                namespace = definition.filter_namespace
                if namespace in callback_namespaces:
                    raise DuplicateCallbackNamespaceError(
                        f"Duplicate callback namespace: {namespace}"
                    )
                callback_namespaces.add(namespace)
        for error_definition in error_definitions:
            if error_definition.name in names:
                raise DuplicateHandlerNameError(
                    f"Duplicate handler name: {error_definition.name}"
                )
            names.add(error_definition.name)
        object.__setattr__(self, "_definitions", ordered)
        object.__setattr__(self, "_error_definitions", error_definitions)

    @property
    def definitions(self) -> tuple[HandlerDefinition, ...]:
        """@brief 返回稳定定义元组 / Return the stable definition tuple.

        @return 不可变 handler 定义 / Immutable handler definitions.
        """

        return self._definitions

    @property
    def error_definitions(self) -> tuple[ErrorHandlerDefinition, ...]:
        """@brief 返回 error group 定义 / Return error-group definitions.

        @return 不可变 error 定义 / Immutable error definitions.
        """

        return self._error_definitions

    def __iter__(self) -> Iterator[HandlerDefinition]:
        """@brief 按稳定声明顺序迭代 / Iterate in stable declaration order.

        @return handler 定义迭代器 / Handler-definition iterator.
        """

        return iter(self._definitions)


def _command(
    name: str,
    command: str,
    callback: HandlerCallback,
) -> HandlerDefinition:
    """@brief 构造命令定义 / Build a command definition.

    @param name 稳定名称 / Stable name.
    @param command Telegram 命令 / Telegram command.
    @param callback 命令 callback / Command callback.
    @return 不可变定义 / Immutable definition.
    """

    return HandlerDefinition(
        name=name,
        kind=HandlerKind.COMMAND,
        filter_namespace=command,
        handler=CommandHandler(command, callback),
    )


def _callback(
    name: str,
    namespace: str,
    pattern: str,
    callback: HandlerCallback,
) -> HandlerDefinition:
    """@brief 构造 callback-query 定义 / Build a callback-query definition.

    @param name 稳定名称 / Stable name.
    @param namespace callback 数据命名空间 / Callback-data namespace.
    @param pattern PTB regex pattern / PTB regex pattern.
    @param callback callback-query callback / Callback-query callback.
    @return 不可变定义 / Immutable definition.
    """

    return HandlerDefinition(
        name=name,
        kind=HandlerKind.CALLBACK,
        filter_namespace=namespace,
        handler=CallbackQueryHandler(callback, pattern=pattern),
    )


def _message(
    name: str,
    namespace: str,
    message_filter: filters.BaseFilter,
    callback: HandlerCallback,
) -> HandlerDefinition:
    """@brief 构造 message 定义 / Build a message definition.

    @param name 稳定名称 / Stable name.
    @param namespace 过滤命名空间 / Filter namespace.
    @param message_filter PTB message filter / PTB message filter.
    @param callback message callback / Message callback.
    @return 不可变定义 / Immutable definition.
    """

    return HandlerDefinition(
        name=name,
        kind=HandlerKind.MESSAGE,
        filter_namespace=namespace,
        handler=MessageHandler(message_filter, callback),
    )


def _chat_member(
    name: str,
    namespace: str,
    callback: HandlerCallback,
) -> HandlerDefinition:
    """@brief 构造 chat-member 定义 / Build a chat-member definition.

    @param name 稳定名称 / Stable name.
    @param namespace 过滤命名空间 / Filter namespace.
    @param callback chat-member callback / Chat-member callback.
    @return 不可变定义 / Immutable definition.
    """

    return HandlerDefinition(
        name=name,
        kind=HandlerKind.CHAT_MEMBER,
        filter_namespace=namespace,
        handler=ChatMemberHandler(
            callback,
            chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER,
        ),
    )


HANDLER_CATALOG = HandlerCatalog(
    (
        _command("basic.start", "start", economy_handlers.start_command),
        _command("monitor.start", "start_test_monitor", start_btc_monitor),
        _command("monitor.stop", "stop_test_monitor", stop_btc_monitor),
        _command("economy.task", "task", economy_handlers.task_command),
        _callback(
            "economy.task-callback",
            "task",
            r"^task_",
            economy_handlers.task_callback,
        ),
        _command(
            "verification.command", "verify", verification_handlers.verify_command
        ),
        _message(
            "verification.new-member",
            "verification.new-chat-members",
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            verification_handlers.new_member_handler,
        ),
        _callback(
            "verification.callback",
            "verify",
            r"^verify:",
            verification_handlers.verify_callback,
        ),
        _message(
            "verification.member-left",
            "verification.left-chat-member",
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            verification_handlers.handle_member_left,
        ),
        _chat_member(
            "membership.bot-status",
            "membership.my-chat-member",
            bot_membership_changed,
        ),
        _command("moderation.keyword", "keyword", moderation_handlers.keyword_command),
        _command("moderation.spam", "spam", moderation_handlers.toggle_spam_control),
        _callback(
            "moderation.spam-help",
            "spam_help",
            r"^spam_help$",
            moderation_handlers.spam_help_callback,
        ),
        _command("game.omikuji", "omikuji", omikuji.omikuji_command),
        _callback(
            "game.omikuji-callback",
            "omikuji",
            r"^(?:omikuji:|omikuji_)",
            omikuji.omikuji_callback,
        ),
        _command("economy.referral", "ref", economy_handlers.ref_command),
        _callback(
            "economy.referral-callback",
            "ref",
            r"^ref_",
            economy_handlers.ref_callback,
        ),
        _command("economy.checkin", "checkin", economy_handlers.checkin_command),
        _command("moderation.report", "report", moderation_handlers.report_command),
        _command("crypto.chart", "chart", crypto_chart.chart_command),
        _command("media.picture", "pic", media_picture.pic_command),
        _command("media.music", "music", media_music.music_command),
        _callback(
            "media.music-callback",
            "music",
            r"^music_",
            media_music.music_callback,
        ),
        _command(
            "admin.web-password",
            "webpassword",
            economy_handlers.webpassword_command,
        ),
    ),
    errors=(ErrorHandlerDefinition("errors.default", telegram_error_handler),),
)
"""@brief 进程级唯一 handler 目录 / Process-wide authoritative handler catalog."""


def install_error_policy(
    application: TelegramApplication,
    catalog: HandlerCatalog = HANDLER_CATALOG,
) -> None:
    """@brief 仅安装 catalog 声明的 PTB error policy / Install only the PTB error policy declared by the catalog.

    @param application PTB Application / PTB Application.
    @param catalog 已校验目录 / Validated catalog.
    @return None / None.
    @note feature definitions 由 durable catalog dispatcher 直接消费，绝不注册到
    PTB Application 形成第二条执行路径。/ Feature definitions are consumed directly
    by the durable catalog dispatcher and are never registered on the PTB Application
    as a second execution path.
    """

    for error_definition in catalog.error_definitions:
        application.add_error_handler(
            cast(
                Callable[[object, Any], Coroutine[Any, Any, None]],
                error_definition.callback,
            )
        )


__all__ = [
    "HANDLER_CATALOG",
    "DuplicateCallbackNamespaceError",
    "DuplicateCommandError",
    "DuplicateHandlerNameError",
    "ErrorHandlerDefinition",
    "HandlerCatalog",
    "HandlerDefinition",
    "HandlerKind",
    "TelegramApplication",
    "install_error_policy",
]
