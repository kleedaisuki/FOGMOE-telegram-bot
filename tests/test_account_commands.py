"""@brief Account service 与 durable Telegram handlers 测试 / Tests for the account service and durable Telegram handlers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.accounts.operations import (
    AccountCode,
    AccountProfile,
    AccountRegistrationResult,
    AccountService,
    PersonalInfoCommand,
    PersonalInfoResult,
    RegisterAccount,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.account_handlers import (
    AccountTelegramCommandHandler,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定接收时刻 / Fixed receipt time."""


class RecordingAccountOperations:
    """@brief 记录账户 commands / Record account commands."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recordings."""

        self.registrations: list[RegisterAccount] = []
        """@brief registration commands / Registration commands."""
        self.personal_commands: list[PersonalInfoCommand] = []
        """@brief personal-info commands / Personal-info commands."""

    async def register(
        self,
        command: RegisterAccount,
    ) -> AccountRegistrationResult:
        """@brief 记录注册 / Record registration.

        @param command registration command / Registration command.
        @return profile snapshot / Profile snapshot.
        """

        self.registrations.append(command)
        return AccountRegistrationResult(
            AccountProfile(
                user_id=command.user_id,
                username=command.username,
                permission=0,
                plan=AccountPlan.FREE,
                free_coins=command.initial_coins,
                paid_coins=0,
            )
        )

    async def personal_info(self, command: PersonalInfoCommand) -> PersonalInfoResult:
        """@brief 记录个人信息命令 / Record a personal-info command.

        @param command personal-info command / Personal-info command.
        @return stable result / Stable result.
        """

        self.personal_commands.append(command)
        current = "old" if command.new_info is None else command.new_info
        return PersonalInfoResult(
            AccountCode.SUCCESS,
            previous_info="old",
            current_info=current,
            updated=command.new_info is not None,
        )


class RecordingOutbound:
    """@brief 记录 responses / Record responses."""

    def __init__(self) -> None:
        """@brief 初始化空记录 / Initialize an empty recording."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief outbound commands / Outbound commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录 response / Record a response.

        @param command outbound command / Outbound command.
        @return None / None.
        """

        self.commands.append(command)


def _service(operations: RecordingAccountOperations) -> AccountService:
    """@brief 构造账户 service / Build the account service.

    @param operations recording operations / Recording operations.
    @return account service / Account service.
    """

    return AccountService(operations, initial_coins=20)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable Update / Build a durable Update.

    @param update_id Update ID / Update ID.
    @return pending Update / Pending Update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(
    name: str,
    *,
    username: str | None = "klee",
    argument_text: str = "",
) -> ParsedTelegramCommand:
    """@brief 构造 parsed command / Build a parsed command.

    @param name command name / Command name.
    @param username optional username / Optional username.
    @param argument_text raw arguments / Raw arguments.
    @return parsed envelope / Parsed envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=42,
        chat_id=42,
        message_id=9,
        message_thread_id=None,
        username=username,
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
    )


def test_account_service_normalizes_configuration_into_command() -> None:
    """@brief service 将配置与输入规范为 typed command / The service normalizes configuration and input into a typed command."""

    operations = RecordingAccountOperations()
    result = asyncio.run(
        _service(operations).register(
            42,
            "@klee",
            idempotency_key="telegram:register:1:42",
        )
    )

    command = operations.registrations[0]
    assert command.username == "klee"
    assert command.initial_coins == 20
    assert result.profile.total_coins == 20


def test_me_handler_writes_profile_to_deterministic_outbox() -> None:
    """@brief `/me` 注册后只写 deterministic outbox / `/me` only writes a deterministic outbox response after registration."""

    operations = RecordingAccountOperations()
    outbound = RecordingOutbound()
    handler = AccountTelegramCommandHandler(
        accounts=_service(operations),
        outbound=outbound,
    )

    asyncio.run(handler.handle(_update(10), _command("me")))

    assert operations.registrations[0].idempotency_key == (
        "telegram:account-register:10:42"
    )
    response = outbound.commands[0]
    assert response.idempotency_key == "update:10:command:me:response"
    assert "总额 Total: 20" in str(response.payload["text"])


def test_setmyinfo_clear_is_an_explicit_empty_update() -> None:
    """@brief CLEAR 与仅查看由 None/空串清楚区分 / CLEAR and inspection are distinguished by empty string versus None."""

    operations = RecordingAccountOperations()
    outbound = RecordingOutbound()
    handler = AccountTelegramCommandHandler(
        accounts=_service(operations),
        outbound=outbound,
    )

    asyncio.run(
        handler.handle(
            _update(11),
            _command("setmyinfo", argument_text="CLEAR"),
        )
    )

    assert operations.personal_commands[0].new_info == ""
    assert operations.personal_commands[0].idempotency_key == (
        "telegram:personal-info:11:42"
    )
    assert "已更新" in str(outbound.commands[0].payload["text"])


def test_me_without_username_never_mutates_account() -> None:
    """@brief 缺少 username 时仅回复设置指引 / Missing username only produces setup guidance."""

    operations = RecordingAccountOperations()
    outbound = RecordingOutbound()
    handler = AccountTelegramCommandHandler(
        accounts=_service(operations),
        outbound=outbound,
    )

    asyncio.run(handler.handle(_update(12), _command("me", username=None)))

    assert operations.registrations == []
    assert "Telegram username" in str(outbound.commands[0].payload["text"])
