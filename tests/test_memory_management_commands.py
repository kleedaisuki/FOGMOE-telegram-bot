"""@brief Telegram Memory/Profile 管理命令测试 / Telegram Memory/Profile management command tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.memory import ForgetMemory, ForgetMemoryResult
from fogmoe_bot.application.telegram import (
    DurableGroupAdministratorAuthorization,
    GroupAdministratorDecision,
)
from fogmoe_bot.application.user_profile import (
    ClearUserProfile,
    RequestUserProfileRegeneration,
    UserProfileManagementResult,
)
from fogmoe_bot.domain.conversation.identity import UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    parse_telegram_command,
)
from fogmoe_bot.presentation.telegram.memory_handlers import (
    MemoryManagementTelegramCommandHandler,
)


NOW = datetime(2037, 4, 5, 6, 7, tzinfo=UTC)
"""@brief 固定命令时刻 / Fixed command timestamp."""


class _Memories:
    """@brief 记录遗忘命令的 Memory fake / Memory fake recording forgetting commands."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize records."""

        self.commands: list[ForgetMemory] = []

    async def forget(self, command: ForgetMemory) -> ForgetMemoryResult:
        """@brief 记录命令 / Record a command.

        @param command 遗忘命令 / Forgetting command.
        @return handler 不解释的占位回执 / Placeholder receipt ignored by the handler.
        """

        self.commands.append(command)
        return cast(ForgetMemoryResult, object())


class _Profiles:
    """@brief 记录 Profile 管理命令的 fake / Fake recording Profile-management commands."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize records."""

        self.clears: list[ClearUserProfile] = []
        self.regenerations: list[RequestUserProfileRegeneration] = []

    async def clear(self, command: ClearUserProfile) -> UserProfileManagementResult:
        """@brief 记录清除命令 / Record a clearing command.

        @param command 清除命令 / Clearing command.
        @return 占位回执 / Placeholder receipt.
        """

        self.clears.append(command)
        return cast(UserProfileManagementResult, object())

    async def request_regeneration(
        self,
        command: RequestUserProfileRegeneration,
    ) -> UserProfileManagementResult:
        """@brief 记录更新请求 / Record a refresh request.

        @param command 更新请求 / Refresh request.
        @return 占位回执 / Placeholder receipt.
        """

        self.regenerations.append(command)
        return cast(UserProfileManagementResult, object())


class _Outbound:
    """@brief 记录非变更反馈的 outbox fake / Outbox fake recording non-mutating feedback."""

    def __init__(self) -> None:
        """@brief 初始化反馈 / Initialize feedback records."""

        self.commands: list[StandaloneOutboundCommand] = []

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录反馈 / Record feedback.

        @param command standalone outbound / Standalone outbound.
        @return None / None.
        """

        self.commands.append(command)


class _AdminSource:
    """@brief 可控 Telegram 管理员来源 / Controllable Telegram-administrator source."""

    def __init__(self, allowed: bool) -> None:
        """@brief 注入决定 / Inject the decision.

        @param allowed 是否允许 / Whether access is allowed.
        """

        self.allowed = allowed
        self.calls = 0

    async def is_administrator(self, *, chat_id: int, user_id: int) -> bool:
        """@brief 返回注入决定 / Return the injected decision.

        @param chat_id 群 ID / Group identifier.
        @param user_id 用户 ID / User identifier.
        @return 注入值 / Injected value.
        """

        assert chat_id == -900 and user_id == 42
        self.calls += 1
        return self.allowed


class _DecisionStore:
    """@brief 内存 first-writer-wins 授权 store / In-memory first-writer-wins authorization store."""

    def __init__(self) -> None:
        """@brief 初始化空决定 / Initialize without a decision."""

        self.decision: GroupAdministratorDecision | None = None

    async def read(
        self,
        update_id: UpdateId,
    ) -> GroupAdministratorDecision | None:
        """@brief 读取决定 / Read the decision.

        @param update_id Update ID / Update ID.
        @return 当前决定 / Current decision.
        """

        assert update_id == UpdateId(99)
        return self.decision

    async def freeze(
        self,
        decision: GroupAdministratorDecision,
    ) -> GroupAdministratorDecision:
        """@brief 冻结首个决定 / Freeze the first decision.

        @param decision 候选决定 / Candidate decision.
        @return 规范决定 / Canonical decision.
        """

        self.decision = self.decision or decision
        return self.decision


def _inbound(
    text: str,
    *,
    chat_type: str = "private",
    chat_id: int = 42,
) -> InboundUpdate:
    """@brief 构造 command Update / Build a command Update.

    @param text 完整命令文本 / Full command text.
    @param chat_type Telegram chat type / Telegram chat type.
    @param chat_id chat ID / Chat ID.
    @return durable inbound / Durable inbound.
    """

    token = text.split(maxsplit=1)[0]
    return InboundUpdate.pending(
        update_id=UpdateId(99),
        conversation_id=TelegramConversationAddress(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=42,
            message_thread_id=None,
        ).conversation_id,
        payload={
            "update_id": 99,
            "message": {
                "message_id": 7,
                "date": int(NOW.timestamp()),
                "chat": {"id": chat_id, "type": chat_type},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Klee",
                },
                "text": text,
                "entities": [
                    {
                        "type": "bot_command",
                        "offset": 0,
                        "length": len(token),
                    }
                ],
            },
        },
        received_at=NOW,
    )


def _handler(
    *,
    allowed: bool = True,
) -> tuple[
    MemoryManagementTelegramCommandHandler,
    _Memories,
    _Profiles,
    _Outbound,
    _AdminSource,
]:
    """@brief 装配 handler 与 fakes / Assemble the handler and fakes.

    @param allowed 群管理员决定 / Group-administrator decision.
    @return handler 与四个可观察 fake / Handler and four observable fakes.
    """

    memories = _Memories()
    profiles = _Profiles()
    outbound = _Outbound()
    source = _AdminSource(allowed)
    handler = MemoryManagementTelegramCommandHandler(
        memories=memories,
        profiles=profiles,
        group_authorization=DurableGroupAdministratorAuthorization(
            source=source,
            store=_DecisionStore(),
        ),
        outbound=outbound,
    )
    return handler, memories, profiles, outbound, source


def test_personal_memory_profile_and_regeneration_commands_are_distinct() -> None:
    """@brief 三个个人命令映射到互不混淆的状态边界 / Three personal commands map to distinct state boundaries."""

    async def scenario() -> None:
        """@brief 逐个执行三个命令 / Execute all three commands.

        @return None / None.
        """

        handler, memories, profiles, outbound, _ = _handler()
        for text in ("/resetmem", "/resetprofile", "/regen"):
            update = _inbound(text)
            parsed = parse_telegram_command(update)
            assert parsed is not None
            await handler.handle(update, parsed)

        assert handler.commands == {
            "resetmem",
            "resetprofile",
            "regen",
            "resetgroup",
        }
        assert len(memories.commands) == 1
        assert memories.commands[0].scope.kind == "personal"
        assert memories.commands[0].scope.scope_id == 42
        assert len(profiles.clears) == 1
        assert len(profiles.regenerations) == 1
        assert not outbound.commands
        assert "User Profile" in str(
            profiles.clears[0].confirmation.payload["text"]
        )

    asyncio.run(scenario())


def test_group_reset_requires_group_admin_and_freezes_the_decision() -> None:
    """@brief 群重置仅允许管理员且同 Update 重放不重新读取权限 / Group reset requires an administrator and does not reread authorization on replay."""

    async def scenario() -> None:
        """@brief 验证允许与拒绝路径 / Verify allowed and denied paths.

        @return None / None.
        """

        handler, memories, _, outbound, source = _handler(allowed=True)
        update = _inbound("/resetgroup", chat_type="supergroup", chat_id=-900)
        parsed = parse_telegram_command(update)
        assert parsed is not None
        await handler.handle(update, parsed)
        source.allowed = False
        await handler.handle(update, parsed)

        assert source.calls == 1
        assert len(memories.commands) == 2
        assert all(item.scope.kind == "group" for item in memories.commands)
        assert all(item.scope.scope_id == -900 for item in memories.commands)
        assert not outbound.commands

        denied, denied_memories, _, denied_outbound, _ = _handler(allowed=False)
        await denied.handle(update, parsed)
        assert not denied_memories.commands
        assert "Only the group owner" in str(
            denied_outbound.commands[0].payload["text"]
        )

    asyncio.run(scenario())


def test_group_scope_and_argument_errors_never_mutate_state() -> None:
    """@brief 错误 scope 或参数只产生 durable 反馈 / Invalid scope or arguments only produce durable feedback."""

    async def scenario() -> None:
        """@brief 执行两个错误命令 / Execute two invalid commands.

        @return None / None.
        """

        handler, memories, profiles, outbound, source = _handler()
        for text in ("/resetgroup", "/resetmem now"):
            update = _inbound(text)
            parsed = parse_telegram_command(update)
            assert parsed is not None
            await handler.handle(update, parsed)

        assert not memories.commands
        assert not profiles.clears and not profiles.regenerations
        assert source.calls == 0
        assert len(outbound.commands) == 2

    asyncio.run(scenario())
