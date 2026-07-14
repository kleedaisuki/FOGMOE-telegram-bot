"""@brief Durable Telegram 群组小镇命令测试 / Tests for durable Telegram group-town commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownCode,
    TownResult,
)
from fogmoe_bot.application.town.service import TownService
from fogmoe_bot.domain.conversation.identity import ConversationId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.town.models import Town, TownProjectKind
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)
from fogmoe_bot.presentation.telegram.town_handlers import TownTelegramCommandHandler


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定测试接收时刻 / Fixed test receipt instant."""

TOWN_SCOPE = TownScope(-100_42)
"""@brief 测试群组小镇范围 / Test group-town scope."""


def _town() -> Town:
    """@brief 构造基础小镇快照 / Build a base town snapshot.

    @return 空白测试小镇 / Empty test town.
    """

    return Town(scope=TOWN_SCOPE, title="雾萌小镇", created_at=NOW)


class _Towns:
    """@brief 记录小镇 Telegram 入口命令的服务替身 / Service double recording town Telegram entry commands."""

    def __init__(self) -> None:
        """@brief 初始化所有调用记录 / Initialize every call record.

        @return None / None.
        """

        self.ensures: list[EnsureTown] = []
        """@brief 已收到的小镇创建读取命令 / Received town ensure commands."""
        self.projects: list[CreateTownProject] = []
        """@brief 已收到的项目提议命令 / Received project-proposal commands."""
        self.contributions: list[ContributeToTown] = []
        """@brief 已收到的贡献命令 / Received contribution commands."""
        self.completions: list[CompleteTownProject] = []
        """@brief 已收到的项目结算命令 / Received project-completion commands."""
        self.overviews: list[TownScope] = []
        """@brief 已收到的概览范围 / Received overview scopes."""

    async def ensure_town(self, command: EnsureTown) -> TownResult:
        """@brief 记录小镇读取创建命令 / Record a town ensure command.

        @param command 小镇读取创建命令 / Town ensure command.
        @return 含基础小镇的成功结果 / Success result with base town.
        """

        self.ensures.append(command)
        return TownResult(TownCode.SUCCESS, town=_town())

    async def create_project(self, command: CreateTownProject) -> TownResult:
        """@brief 记录项目提议命令 / Record a project-proposal command.

        @param command 项目提议命令 / Project-proposal command.
        @return 含新项目的成功结果 / Success result with the new project.
        """

        self.projects.append(command)
        project = command.project()
        return TownResult(
            TownCode.SUCCESS,
            town=_town().create_project(project),
            project=project,
        )

    async def contribute(self, command: ContributeToTown) -> TownResult:
        """@brief 记录贡献命令 / Record a contribution command.

        @param command 小镇贡献命令 / Town-contribution command.
        @return 基础成功结果 / Base success result.
        """

        self.contributions.append(command)
        return TownResult(TownCode.SUCCESS, town=_town())

    async def complete_project(self, command: CompleteTownProject) -> TownResult:
        """@brief 记录项目结算命令 / Record a project-completion command.

        @param command 小镇项目结算命令 / Town project-completion command.
        @return 基础成功结果 / Base success result.
        """

        self.completions.append(command)
        return TownResult(TownCode.SUCCESS, town=_town())

    async def overview(self, town: TownScope) -> TownResult:
        """@brief 记录概览读取 / Record an overview read.

        @param town 小镇范围 / Town scope.
        @return 基础成功结果 / Base success result.
        """

        self.overviews.append(town)
        return TownResult(TownCode.SUCCESS, town=_town())


class _Outbound:
    """@brief 记录 durable 回包的 outbox 替身 / Outbox double recording durable replies."""

    def __init__(self) -> None:
        """@brief 初始化空回包记录 / Initialize an empty reply record.

        @return None / None.
        """

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 已入队回包 / Enqueued replies."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一条 outbox 回包 / Record one outbox reply.

        @param command durable outbox 命令 / Durable outbox command.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造一个 durable 来源 Update / Build one durable source update.

    @param update_id Update 标识 / Update identity.
    @return 待处理 Update / Pending update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("telegram-group:-100_42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(
    argument_text: str = "",
    *,
    chat_type: str = "supergroup",
) -> ParsedTelegramCommand:
    """@brief 构造已解析 `/town` 命令 / Build a parsed `/town` command.

    @param argument_text 原始参数文本 / Raw argument text.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @return 已解析命令 envelope / Parsed command envelope.
    """

    return ParsedTelegramCommand(
        command="town",
        target=None,
        user_id=42,
        chat_id=-100_42 if chat_type != "private" else 42,
        message_id=9,
        message_thread_id=None,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def test_group_town_commands_preserve_explicit_group_and_personal_scopes() -> None:
    """@brief 群组命令构造显式小镇/个人范围并使用 durable 回包 / Group commands construct explicit town/personal scopes and use durable replies.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行小镇命令场景 / Execute the town-command scenario.

        @return None / None.
        """

        towns = _Towns()
        outbound = _Outbound()
        handler = TownTelegramCommandHandler(
            towns=cast(TownService, towns),
            outbound=outbound,
        )

        assert handler.commands == frozenset({"town"})
        await handler.handle(_update(80), _command())
        assert towns.ensures[0].town == TOWN_SCOPE
        assert towns.ensures[0].idempotency_key == "telegram:town:ensure:80:42"
        assert "雾萌小镇" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(81), _command("project garden 8 月光花园"))
        project = towns.projects[0]
        assert project.town == TOWN_SCOPE
        assert project.proposer.user_id == 42
        assert project.kind is TownProjectKind.GARDEN
        assert project.required_amount.value == 8
        assert project.idempotency_key == "telegram:town:project:81:42"
        assert "已提议项目" in str(outbound.commands[-1].payload["text"])

        project_id = uuid4()
        await handler.handle(_update(82), _command(f"contribute 3 {project_id}"))
        contribution = towns.contributions[0]
        assert contribution.town == TOWN_SCOPE
        assert contribution.contributor.user_id == 42
        assert contribution.amount.value == 3
        assert contribution.project_id == project_id
        assert contribution.idempotency_key == "telegram:town:contribute:82:42"

        await handler.handle(_update(83), _command(f"complete {project_id}"))
        completion = towns.completions[0]
        assert completion.town == TOWN_SCOPE
        assert completion.operator.user_id == 42
        assert completion.project_id == project_id
        assert completion.idempotency_key == "telegram:town:complete:83:42"
        assert outbound.commands[0].idempotency_key == "update:80:command:town:response"

    asyncio.run(scenario())


def test_private_town_call_never_reaches_town_service() -> None:
    """@brief 私聊调用不会构造群组小镇或触达服务 / A private call constructs no group town and never reaches the service.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行私聊边界场景 / Execute the private-boundary scenario.

        @return None / None.
        """

        towns = _Towns()
        outbound = _Outbound()
        handler = TownTelegramCommandHandler(
            towns=cast(TownService, towns),
            outbound=outbound,
        )

        await handler.handle(_update(84), _command(chat_type="private"))

        assert towns.ensures == []
        assert towns.projects == []
        assert towns.contributions == []
        assert towns.completions == []
        assert "只能在群聊" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())
