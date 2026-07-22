"""@brief Durable Telegram 个人 RPG 命令测试 / Durable Telegram personal-RPG command tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgCode,
    PersonalRpgResult,
)
from fogmoe_bot.application.personal_rpg.service import PersonalRpgService
from fogmoe_bot.domain.conversation.identity import ConversationId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.personal_rpg.catalog import MaterialInventory
from fogmoe_bot.domain.personal_rpg.character import PersonalCharacter
from fogmoe_bot.domain.personal_rpg.profile import PersonalRpgProfile
from fogmoe_bot.domain.world.scope import PersonalScope
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)
from fogmoe_bot.presentation.telegram.personal_rpg_handlers import (
    PersonalRpgTelegramCommandHandler,
)

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定测试接收时刻 / Fixed test receipt instant."""

SCOPE = PersonalScope(42)
"""@brief 测试个人范围 / Test personal scope."""


def _profile(scope: PersonalScope = SCOPE) -> PersonalRpgProfile:
    """@brief 创建基础测试个人 RPG 档案 / Build base test personal-RPG profile.

    @param scope 角色个人范围 / Character personal scope.
    @return 空白个人 RPG 档案 / Blank personal-RPG profile.
    """

    return PersonalRpgProfile(PersonalCharacter(scope=scope, name="可莉"))


class _Operations:
    """@brief 记录个人 RPG Telegram 命令的内存端口 / In-memory port recording personal-RPG Telegram commands."""

    def __init__(self, *, registered: bool = True) -> None:
        """@brief 初始化调用记录和注册状态 / Initialize call records and registration state.

        @param registered 是否存在个人角色 / Whether a personal character exists.
        """

        self.registered = registered
        """@brief 个人角色是否存在 / Whether a personal character exists."""
        self.creations: list[CreatePersonalCharacter] = []
        """@brief 已收到创建角色命令 / Received character-creation commands."""
        self.explorations: list[ExploreDaily] = []
        """@brief 已收到每日探索命令 / Received daily-exploration commands."""
        self.crafts: list[CraftPersonalRecipe] = []
        """@brief 已收到制作命令 / Received crafting commands."""
        self.overviews: list[PersonalScope] = []
        """@brief 已收到概览读取 / Received overview reads."""

    async def create_character(
        self,
        command: CreatePersonalCharacter,
    ) -> PersonalRpgResult:
        """@brief 记录并返回创建结果 / Record and return character-creation result.

        @param command 创建角色命令 / Character-creation command.
        @return 成功或已存在结果 / Success or already-exists result.
        """

        self.creations.append(command)
        if self.registered:
            return PersonalRpgResult(
                PersonalRpgCode.ALREADY_EXISTS,
                profile=_profile(command.scope),
            )
        self.registered = True
        return PersonalRpgResult(PersonalRpgCode.SUCCESS, profile=command.profile())

    async def explore_daily(self, command: ExploreDaily) -> PersonalRpgResult:
        """@brief 记录并结算固定探索 / Record and settle fixed exploration.

        @param command 每日探索命令 / Daily-exploration command.
        @return 成功或未注册结果 / Success or not-registered result.
        """

        self.explorations.append(command)
        if not self.registered:
            return PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
        exploration = command.exploration()
        profile = _profile(command.scope).apply_exploration(exploration)
        return PersonalRpgResult(
            PersonalRpgCode.SUCCESS,
            profile=profile,
            exploration=exploration,
        )

    async def craft_recipe(self, command: CraftPersonalRecipe) -> PersonalRpgResult:
        """@brief 记录并结算固定配方制作 / Record and settle fixed recipe crafting.

        @param command 制作配方命令 / Recipe-crafting command.
        @return 成功或未注册结果 / Success or not-registered result.
        """

        self.crafts.append(command)
        if not self.registered:
            return PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
        recipe = command.recipe()
        profile = PersonalRpgProfile(
            character=PersonalCharacter(scope=command.scope, name="可莉"),
            materials=MaterialInventory(recipe.ingredients.quantities),
        ).craft(recipe)
        return PersonalRpgResult(
            PersonalRpgCode.SUCCESS,
            profile=profile,
            recipe=recipe,
        )

    async def overview(self, scope: PersonalScope) -> PersonalRpgResult:
        """@brief 记录并返回个人档案 / Record and return personal profile.

        @param scope 个人范围 / Personal scope.
        @return 成功或未注册结果 / Success or not-registered result.
        """

        self.overviews.append(scope)
        if not self.registered:
            return PersonalRpgResult(PersonalRpgCode.NOT_REGISTERED)
        return PersonalRpgResult(PersonalRpgCode.SUCCESS, profile=_profile(scope))


class _Outbound:
    """@brief 记录 durable Telegram 回包 / Record durable Telegram replies."""

    def __init__(self) -> None:
        """@brief 初始化空回包记录 / Initialize empty reply recording.

        @return None / None.
        """

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 已入队的 outbox 命令 / Enqueued outbox commands."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一条 durable 回包 / Record one durable reply.

        @param command standalone outbox 命令 / Standalone-outbox command.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造私聊 durable Update / Build a private durable update.

    @param update_id Update 标识 / Update identity.
    @return 待处理 durable Update / Pending durable update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(
    name: str,
    argument_text: str = "",
    *,
    chat_type: str = "private",
) -> ParsedTelegramCommand:
    """@brief 构造已解析个人冒险命令 / Build a parsed personal-adventure command.

    @param name 无 slash 命令名 / Command name without slash.
    @param argument_text 原始参数文本 / Raw argument text.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @return parsed 命令 envelope / Parsed command envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=42,
        chat_id=42 if chat_type == "private" else -100_42,
        message_id=9,
        message_thread_id=None,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def test_private_adventure_commands_build_typed_commands_and_durable_replies() -> None:
    """@brief 私聊冒险命令构造 typed 命令并写 durable 回包 / Private adventure commands build typed commands and durable replies.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行私聊冒险命令场景 / Execute private adventure command scenario.

        @return None / None.
        """

        operations = _Operations(registered=False)
        outbound = _Outbound()
        handler = PersonalRpgTelegramCommandHandler(
            personal_rpg=PersonalRpgService(operations=operations),
            outbound=outbound,
        )

        assert handler.commands == frozenset(
            {
                "adventure",
                "adventure_create",
                "adventure_explore",
                "adventure_craft",
                "adventure_collection",
            }
        )

        await handler.handle(_update(30), _command("adventure_create", "可莉"))
        assert operations.creations[0].scope == SCOPE
        assert operations.creations[0].name == "可莉"
        assert (
            operations.creations[0].idempotency_key
            == "telegram:personal-rpg:adventure_create:30:42"
        )
        assert "创建成功" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(31), _command("adventure_explore", "woodland"))
        exploration = operations.explorations[0]
        assert exploration.scope == SCOPE
        assert exploration.day == NOW.date()
        assert exploration.explored_at == NOW
        assert exploration.route.value == "woodland"
        assert (
            exploration.idempotency_key
            == "telegram:personal-rpg:adventure_explore:31:42"
        )
        assert "今日林地探索完成" in str(outbound.commands[-1].payload["text"])
        assert "审计摘要" in str(outbound.commands[-1].payload["text"])

        await handler.handle(
            _update(32),
            _command("adventure_craft", "药草灯笼"),
        )
        craft = operations.crafts[0]
        assert craft.scope == SCOPE
        assert craft.recipe_code.value == "herbal_lantern"
        assert "制作成功：药草灯笼" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(33), _command("adventure"))
        assert operations.overviews[-1] == SCOPE
        assert "个人冒险档案" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(34), _command("adventure_collection"))
        assert operations.overviews[-1] == SCOPE
        assert "个人收藏图鉴" in str(outbound.commands[-1].payload["text"])
        assert (
            outbound.commands[0].idempotency_key
            == "update:30:command:adventure_create:response"
        )

    asyncio.run(scenario())


def test_group_adventure_commands_never_reach_personal_rpg_service() -> None:
    """@brief 群聊冒险命令不会触达个人 RPG 服务 / Group adventure commands never reach personal-RPG service.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行群聊边界场景 / Execute group-boundary scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = PersonalRpgTelegramCommandHandler(
            personal_rpg=PersonalRpgService(operations=operations),
            outbound=outbound,
        )

        await handler.handle(
            _update(35),
            _command("adventure_explore", "woodland", chat_type="supergroup"),
        )

        assert operations.creations == []
        assert operations.explorations == []
        assert operations.crafts == []
        assert operations.overviews == []
        assert "仅限私聊" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_invalid_adventure_route_and_recipe_do_not_call_service() -> None:
    """@brief 非法路线和配方不会调用个人 RPG 服务 / Invalid route and recipe do not call personal-RPG service.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行参数校验场景 / Execute argument-validation scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = PersonalRpgTelegramCommandHandler(
            personal_rpg=PersonalRpgService(operations=operations),
            outbound=outbound,
        )

        await handler.handle(_update(36), _command("adventure_explore", "volcano"))
        await handler.handle(_update(37), _command("adventure_craft", "unknown"))

        assert operations.explorations == []
        assert operations.crafts == []
        assert "未知路线" in str(outbound.commands[0].payload["text"])
        assert "未知配方" in str(outbound.commands[1].payload["text"])

    asyncio.run(scenario())


def test_adventure_overview_guides_unregistered_player_to_character_creation() -> None:
    """@brief 未注册玩家会获得创建角色引导 / Unregistered player receives character-creation guidance.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行未注册概览场景 / Execute unregistered-overview scenario.

        @return None / None.
        """

        operations = _Operations(registered=False)
        outbound = _Outbound()
        handler = PersonalRpgTelegramCommandHandler(
            personal_rpg=PersonalRpgService(operations=operations),
            outbound=outbound,
        )

        await handler.handle(_update(38), _command("adventure"))

        assert operations.overviews == [SCOPE]
        assert "/adventure_create" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())
