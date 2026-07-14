"""@brief Durable Telegram 个人 RPG 命令 / Durable Telegram personal-RPG commands."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from types import MappingProxyType
from typing import Final
from uuid import uuid4

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgCode,
    PersonalRpgResult,
)
from fogmoe_bot.application.personal_rpg.service import PersonalRpgService
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.personal_rpg.catalog import (
    CollectibleKind,
    MaterialKind,
    RecipeCode,
)
from fogmoe_bot.domain.personal_rpg.exploration import ExplorationRoute
from fogmoe_bot.domain.personal_rpg.profile import PersonalRpgProfile
from fogmoe_bot.domain.world.scope import PersonalScope

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


_PRIVATE_ONLY_TEXT: Final[str] = "个人冒险仅限私聊使用喵，请私聊 Bot 后再试。"
"""@brief 个人 RPG 私聊边界提示 / Personal-RPG private-chat boundary prompt."""

_ROUTE_ALIASES: Final[Mapping[str, ExplorationRoute]] = MappingProxyType(
    {
        "woodland": ExplorationRoute.WOODLAND,
        "forest": ExplorationRoute.WOODLAND,
        "林地": ExplorationRoute.WOODLAND,
        "quarry": ExplorationRoute.QUARRY,
        "采石场": ExplorationRoute.QUARRY,
        "shore": ExplorationRoute.SHORE,
        "海岸": ExplorationRoute.SHORE,
    }
)
"""@brief 用户可输入路线别名到固定路线的映射 / Mapping from user-entered route aliases to fixed routes."""

_RECIPE_ALIASES: Final[Mapping[str, RecipeCode]] = MappingProxyType(
    {
        "herbal_lantern": RecipeCode.HERBAL_LANTERN,
        "药草灯笼": RecipeCode.HERBAL_LANTERN,
        "rune_charm": RecipeCode.RUNE_CHARM,
        "符文护符": RecipeCode.RUNE_CHARM,
        "tidal_mobile": RecipeCode.TIDAL_MOBILE,
        "潮汐风铃": RecipeCode.TIDAL_MOBILE,
    }
)
"""@brief 用户可输入配方别名到固定配方的映射 / Mapping from user-entered recipe aliases to fixed recipes."""

_ROUTE_LABELS: Final[Mapping[ExplorationRoute, str]] = MappingProxyType(
    {
        ExplorationRoute.WOODLAND: "林地",
        ExplorationRoute.QUARRY: "采石场",
        ExplorationRoute.SHORE: "海岸",
    }
)
"""@brief 固定路线的中文展示名 / Chinese display names for fixed routes."""

_MATERIAL_LABELS: Final[Mapping[MaterialKind, str]] = MappingProxyType(
    {
        MaterialKind.FIBER: "纤维",
        MaterialKind.HERB: "药草",
        MaterialKind.STONE: "石料",
        MaterialKind.ORE: "矿石",
        MaterialKind.SHELL: "贝壳",
        MaterialKind.ALGAE: "海藻",
    }
)
"""@brief 材料类别的中文展示名 / Chinese display names for material kinds."""

_COLLECTIBLE_LABELS: Final[Mapping[CollectibleKind, str]] = MappingProxyType(
    {
        CollectibleKind.HERBAL_LANTERN: "药草灯笼",
        CollectibleKind.RUNE_CHARM: "符文护符",
        CollectibleKind.TIDAL_MOBILE: "潮汐风铃",
    }
)
"""@brief 收藏品类别的中文展示名 / Chinese display names for collectible kinds."""


class PersonalRpgTelegramCommandHandler:
    """@brief 将私聊冒险命令映射到 typed service 与 durable outbox / Map private adventure commands to typed service and durable outbox.

    Telegram chat type 是传输边界：只有 ``private`` 更新会被转换成 ``PersonalScope``。
    ``supergroup``、``group``、``channel`` 和 topic 均不会调用个人 RPG 服务。
    Telegram chat type is the transport boundary: only a ``private`` update is converted to a
    ``PersonalScope``. ``supergroup``, ``group``, ``channel``, and topics never call the personal
    RPG service.

    @param personal_rpg 个人 RPG 应用服务 / Personal-RPG application service.
    @param outbound durable standalone outbox 能力 / Durable standalone-outbox capability.
    """

    def __init__(
        self,
        *,
        personal_rpg: PersonalRpgService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入个人 RPG 服务和可靠投递能力 / Inject personal-RPG service and durable delivery capability.

        @param personal_rpg 个人 RPG 应用服务 / Personal-RPG application service.
        @param outbound durable standalone outbox 能力 / Durable standalone-outbox capability.
        """

        self._personal_rpg = personal_rpg
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回个人冒险命令所有权 / Return personal-adventure command ownership.

        @return adventure/adventure_create/adventure_explore/adventure_craft/adventure_collection /
            Personal-adventure command set.
        """

        return frozenset(
            {
                "adventure",
                "adventure_create",
                "adventure_explore",
                "adventure_craft",
                "adventure_collection",
            }
        )

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行个人冒险命令并持久化回复 / Execute a personal-adventure command and persist its reply.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析命令 envelope / Parsed command envelope.
        @return None / None.
        """

        if command.chat_type != "private":
            text = _PRIVATE_ONLY_TEXT
        elif command.command == "adventure":
            text = await self._overview_text(command)
        elif command.command == "adventure_create":
            text = await self._create_text(update, command)
        elif command.command == "adventure_explore":
            text = await self._explore_text(update, command)
        elif command.command == "adventure_craft":
            text = await self._craft_text(update, command)
        elif command.command == "adventure_collection":
            text = await self._collection_text(command)
        else:
            raise ValueError("Personal RPG handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _overview_text(self, command: ParsedTelegramCommand) -> str:
        """@brief 读取并渲染个人冒险概览 / Load and render personal-adventure overview.

        @param command 已解析 `/adventure` 命令 / Parsed `/adventure` command.
        @return 用户可见概览文本 / User-facing overview text.
        """

        result = await self._personal_rpg.overview(_scope_for(command))
        return _overview_result_text(result)

    async def _create_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 创建私聊个人角色 / Create a private personal character.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析创建角色命令 / Parsed character-creation command.
        @return 用户可见创建结果 / User-facing creation result.
        """

        name = command.argument_text.strip()
        if not name:
            return "用法：/adventure_create <角色名>\n角色名长度为 1–40 个字符。"
        try:
            typed_command = CreatePersonalCharacter(
                scope=_scope_for(command),
                name=name,
                created_at=update.received_at,
                idempotency_key=_idempotency_key(update, command),
            )
        except TypeError, ValueError:
            return "角色名长度为 1–40 个字符，请换一个名字再试喵。"
        result = await self._personal_rpg.create_character(typed_command)
        return _creation_result_text(result)

    async def _explore_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 结算一次固定奖励的每日探索 / Settle one fixed-reward daily exploration.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析探索命令 / Parsed exploration command.
        @return 用户可见探索结果 / User-facing exploration result.
        """

        route_or_error = _route_argument(command.argument_text)
        if not isinstance(route_or_error, ExplorationRoute):
            return route_or_error
        try:
            typed_command = ExploreDaily(
                exploration_id=uuid4(),
                scope=_scope_for(command),
                day=update.received_at.astimezone(UTC).date(),
                route=route_or_error,
                explored_at=update.received_at,
                idempotency_key=_idempotency_key(update, command),
            )
        except TypeError, ValueError:
            return "探索时间无效，请稍后再试。"
        result = await self._personal_rpg.explore_daily(typed_command)
        return _exploration_result_text(result)

    async def _craft_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 消耗材料制作并收录收藏品 / Consume materials to craft and record a collectible.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析制作命令 / Parsed crafting command.
        @return 用户可见制作结果 / User-facing crafting result.
        """

        recipe_or_error = _recipe_argument(command.argument_text)
        if not isinstance(recipe_or_error, RecipeCode):
            return recipe_or_error
        try:
            typed_command = CraftPersonalRecipe(
                craft_id=uuid4(),
                scope=_scope_for(command),
                recipe_code=recipe_or_error,
                crafted_at=update.received_at,
                idempotency_key=_idempotency_key(update, command),
            )
        except TypeError, ValueError:
            return "制作时间无效，请稍后再试。"
        result = await self._personal_rpg.craft_recipe(typed_command)
        return _craft_result_text(result)

    async def _collection_text(self, command: ParsedTelegramCommand) -> str:
        """@brief 读取并渲染个人收藏图鉴 / Load and render personal collection compendium.

        @param command 已解析 `/adventure_collection` 命令 / Parsed collection command.
        @return 用户可见图鉴文本 / User-facing compendium text.
        """

        result = await self._personal_rpg.overview(_scope_for(command))
        if result.code is PersonalRpgCode.NOT_REGISTERED:
            return _not_registered_text()
        if result.code is not PersonalRpgCode.SUCCESS or result.profile is None:
            return "图鉴暂时无法读取，请稍后再试。"
        return _collection_text(result.profile)


def _scope_for(command: ParsedTelegramCommand) -> PersonalScope:
    """@brief 将已通过私聊边界的命令转换为个人范围 / Convert a command past the private-chat boundary into personal scope.

    @param command 已解析私聊命令 / Parsed private-chat command.
    @return 强类型个人范围 / Strongly typed personal scope.
    @raise ValueError 用户 ID 非法时抛出 / Raised when user identifier is invalid.
    """

    return PersonalScope(command.user_id)


def _idempotency_key(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
) -> str:
    """@brief 构造每个 durable 命令的稳定幂等键 / Build stable idempotency key for one durable command.

    @param update durable 来源 Update / Durable source update.
    @param command 已解析命令 / Parsed command.
    @return 业务操作幂等键 / Business-operation idempotency key.
    """

    return (
        f"telegram:personal-rpg:{command.command}:{int(update.update_id)}:"
        f"{command.user_id}"
    )


def _route_argument(argument_text: str) -> ExplorationRoute | str:
    """@brief 解析 `/adventure_explore <route>` / Parse `/adventure_explore <route>`.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 固定路线，或用户可见用法错误 / Fixed route, or user-visible usage error.
    """

    parts = argument_text.split()
    if len(parts) != 1:
        return "用法：/adventure_explore <路线>\n路线：woodland（林地）、quarry（采石场）、shore（海岸）。"
    route = _ROUTE_ALIASES.get(parts[0].casefold())
    if route is None:
        return "未知路线。可选：woodland（林地）、quarry（采石场）、shore（海岸）。"
    return route


def _recipe_argument(argument_text: str) -> RecipeCode | str:
    """@brief 解析 `/adventure_craft <recipe>` / Parse `/adventure_craft <recipe>`.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 固定配方编码，或用户可见用法错误 / Fixed recipe code, or user-visible usage error.
    """

    parts = argument_text.split()
    if len(parts) != 1:
        return "用法：/adventure_craft <配方>\n配方：herbal_lantern、rune_charm、tidal_mobile。"
    recipe_code = _RECIPE_ALIASES.get(parts[0].casefold())
    if recipe_code is None:
        return "未知配方。可选：herbal_lantern、rune_charm、tidal_mobile。"
    return recipe_code


def _overview_result_text(result: PersonalRpgResult) -> str:
    """@brief 渲染个人冒险概览结果 / Render personal-adventure overview result.

    @param result typed 个人 RPG 结果 / Typed personal-RPG result.
    @return 用户可见概览文本 / User-facing overview text.
    """

    if result.code is PersonalRpgCode.NOT_REGISTERED:
        return _not_registered_text()
    if result.code is not PersonalRpgCode.SUCCESS or result.profile is None:
        return "冒险档案暂时无法读取，请稍后再试。"
    return _profile_text(result.profile)


def _creation_result_text(result: PersonalRpgResult) -> str:
    """@brief 渲染创建角色结果 / Render character-creation result.

    @param result typed 个人 RPG 结果 / Typed personal-RPG result.
    @return 用户可见创建文本 / User-facing character-creation text.
    """

    if result.code is PersonalRpgCode.ALREADY_EXISTS:
        if result.profile is not None:
            return f"你的冒险角色已经存在喵。\n\n{_profile_text(result.profile)}"
        return "你的冒险角色已经存在喵，使用 /adventure 查看档案。"
    if result.code is not PersonalRpgCode.SUCCESS or result.profile is None:
        return "角色创建未完成，请稍后再试。"
    return (
        f"冒险角色「{result.profile.character.name}」创建成功！\n"
        "现在可用 /adventure_explore <路线> 开始今日探索。"
    )


def _exploration_result_text(result: PersonalRpgResult) -> str:
    """@brief 渲染每日探索结算结果 / Render daily-exploration settlement result.

    @param result typed 个人 RPG 结果 / Typed personal-RPG result.
    @return 用户可见探索文本 / User-facing exploration text.
    """

    if result.code is PersonalRpgCode.NOT_REGISTERED:
        return _not_registered_text()
    if result.code is PersonalRpgCode.ALREADY_EXPLORED:
        return "今天已经完成过探索啦，明天再来收集新的材料吧。"
    if result.code is PersonalRpgCode.CONFLICT:
        return "这次探索正在确认中，请稍后用 /adventure 查看档案。"
    if (
        result.code is not PersonalRpgCode.SUCCESS
        or result.exploration is None
        or result.profile is None
    ):
        return "探索未完成，请稍后再试。"
    exploration = result.exploration
    materials = _materials_text(exploration.reward.materials.quantities)
    return (
        f"🧭 今日{_ROUTE_LABELS[exploration.route]}探索完成！\n"
        f"经验 +{exploration.reward.experience}\n"
        f"采集：{materials}\n"
        f"审计摘要：{exploration.audit_digest[:16]}…\n"
        f"当前等级：Lv.{result.profile.character.level}"
    )


def _craft_result_text(result: PersonalRpgResult) -> str:
    """@brief 渲染制作与图鉴收录结果 / Render crafting and compendium-recording result.

    @param result typed 个人 RPG 结果 / Typed personal-RPG result.
    @return 用户可见制作文本 / User-facing crafting text.
    """

    if result.code is PersonalRpgCode.NOT_REGISTERED:
        return _not_registered_text()
    if result.code is PersonalRpgCode.MATERIALS_INSUFFICIENT:
        return "材料不足，先用 /adventure_explore <路线> 收集材料吧。"
    if result.code is PersonalRpgCode.ALREADY_COLLECTED:
        return "这个收藏品已经在图鉴中啦，换一条配方试试吧。"
    if result.code is PersonalRpgCode.CONFLICT:
        return "这次制作正在确认中，请稍后查看 /adventure_collection。"
    if result.code is not PersonalRpgCode.SUCCESS or result.recipe is None:
        return "制作未完成，请稍后再试。"
    collectible = _COLLECTIBLE_LABELS[result.recipe.output]
    return f"✨ 制作成功：{collectible}\n已收录到个人收藏图鉴！"


def _not_registered_text() -> str:
    """@brief 返回尚未创建角色提示 / Return prompt for absent character.

    @return 固定创建角色引导 / Fixed character-creation guidance.
    """

    return "你还没有冒险角色喵。先用 /adventure_create <角色名> 创建一个吧。"


def _profile_text(profile: PersonalRpgProfile) -> str:
    """@brief 渲染个人 RPG 档案 / Render personal-RPG profile.

    @param profile 个人 RPG 进度快照 / Personal-RPG progression snapshot.
    @return 用户可见档案文本 / User-facing profile text.
    """

    progress, required = profile.character.experience_progress
    exploration_text = (
        profile.last_exploration_day.isoformat()
        if profile.last_exploration_day is not None
        else "尚未探索"
    )
    return (
        "🧭 个人冒险档案\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"角色：{profile.character.name}\n"
        f"等级：Lv.{profile.character.level}\n"
        f"经验：{profile.character.experience}"
        f"（本级 {progress}/{required}）\n"
        f"上次探索（UTC）：{exploration_text}\n"
        f"材料：{_materials_text(profile.materials.quantities)}\n"
        f"图鉴：{profile.compendium.completed_count}/{profile.compendium.total_count}\n\n"
        "探索：/adventure_explore <woodland|quarry|shore>\n"
        "制作：/adventure_craft <recipe>\n"
        "图鉴：/adventure_collection"
    )


def _collection_text(profile: PersonalRpgProfile) -> str:
    """@brief 渲染个人收藏图鉴 / Render personal collection compendium.

    @param profile 个人 RPG 进度快照 / Personal-RPG progression snapshot.
    @return 用户可见图鉴文本 / User-facing compendium text.
    """

    lines = [
        "📚 个人收藏图鉴",
        "━━━━━━━━━━━━━━━━━━",
        f"完成度：{profile.compendium.completed_count}/{profile.compendium.total_count}",
    ]
    for collectible in CollectibleKind:
        marker = "✅" if profile.compendium.contains(collectible) else "▫️"
        lines.append(f"{marker} {_COLLECTIBLE_LABELS[collectible]}")
    return "\n".join(lines)


def _materials_text(quantities: Mapping[MaterialKind, int]) -> str:
    """@brief 渲染材料数量映射 / Render material-quantity mapping.

    @param quantities 材料到数量的映射 / Mapping from material kinds to quantities.
    @return 用户可见材料文本 / User-facing material text.
    @note 调用点传入经过领域校验的不可变映射；Telegram 层不拥有库存变更逻辑。/
        Callers provide a domain-validated immutable mapping; the Telegram layer owns no
        inventory-mutation logic.
    """

    pairs = tuple(quantities.items())
    if not pairs:
        return "暂无"
    rendered: list[str] = []
    for kind, quantity in pairs:
        if not isinstance(kind, MaterialKind):
            raise TypeError("Material quantity key must be MaterialKind")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("Material quantity must be positive")
        rendered.append(f"{_MATERIAL_LABELS[kind]} ×{quantity}")
    return "、".join(rendered)


__all__ = ["PersonalRpgTelegramCommandHandler"]
"""@brief 对外导出的个人 RPG Telegram 命令处理器 / Exported personal-RPG Telegram command handler."""
