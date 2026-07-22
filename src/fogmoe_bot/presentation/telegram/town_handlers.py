"""@brief Durable Telegram 群组小镇命令 / Durable Telegram group-town commands."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

from telegram import Bot
from telegram.error import TelegramError

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownCode,
    TownResult,
)
from fogmoe_bot.application.town.ports import TownAuthorization
from fogmoe_bot.application.town.service import TownService
from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.town.models import Town, TownProjectKind, TownProjectStatus
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


_GROUP_ONLY_TEXT = (
    "群组小镇只能在群聊或超级群中使用喵；个人冒险请私聊后使用 /adventure。"
)
"""@brief 非群组调用小镇时的固定提示 / Fixed prompt for non-group town calls."""

_PROJECT_KIND_ALIASES: Mapping[str, TownProjectKind] = {
    "hall": TownProjectKind.COMMUNITY_HALL,
    "community_hall": TownProjectKind.COMMUNITY_HALL,
    "会馆": TownProjectKind.COMMUNITY_HALL,
    "workshop": TownProjectKind.WORKSHOP,
    "工坊": TownProjectKind.WORKSHOP,
    "garden": TownProjectKind.GARDEN,
    "花园": TownProjectKind.GARDEN,
    "observatory": TownProjectKind.OBSERVATORY,
    "观测站": TownProjectKind.OBSERVATORY,
}
"""@brief Telegram 输入到项目类型的稳定别名表 / Stable aliases from Telegram input to project kinds."""

_PROJECT_KIND_TEXT: Mapping[TownProjectKind, str] = {
    TownProjectKind.COMMUNITY_HALL: "公共会馆",
    TownProjectKind.WORKSHOP: "协作工坊",
    TownProjectKind.GARDEN: "社区花园",
    TownProjectKind.OBSERVATORY: "观测站",
}
"""@brief 项目类别的成员可见名称 / Member-visible names for project kinds."""

_PROJECT_STATUS_TEXT: Mapping[TownProjectStatus, str] = {
    TownProjectStatus.FUNDING: "筹资中",
    TownProjectStatus.READY: "待建成",
    TownProjectStatus.COMPLETED: "已建成",
}
"""@brief 项目状态的成员可见名称 / Member-visible names for project statuses."""


class TelegramTownAuthorization(TownAuthorization):
    """@brief 以 Telegram ChatMember 做群组成员与治理授权 / Authorize group members and managers through Telegram ChatMember.

    @note 这个适配器只做入站提前拒绝。数据库端口仍以显式 ``TownScope``、
        ``PersonalScope``、账本锁和回执保证原子性。/ This adapter only performs early
        ingress rejection. The database port still guarantees atomicity through explicit scopes,
        ledger locks, and receipts.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 注入 Telegram Bot 客户端 / Inject the Telegram Bot client.

        @param bot Telegram Bot 客户端 / Telegram Bot client.
        """

        self._bot = bot
        """@brief 查询 ChatMember 的 Telegram 客户端 / Telegram client used to query ChatMember."""

    async def may_contribute(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 判断成员能否贡献到群组金库 / Check whether a member may contribute to a group treasury.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 当前有效成员时为 True / True when currently an effective member.
        """

        return await self._member_status(actor=actor, town=town) in {
            "creator",
            "owner",
            "administrator",
            "admin",
            "member",
            "restricted",
        }

    async def may_manage(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 判断成员能否治理小镇项目 / Check whether a member may manage town projects.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 群主或管理员时为 True / True for the owner or an administrator.
        """

        return await self._member_status(actor=actor, town=town) in {
            "creator",
            "owner",
            "administrator",
            "admin",
        }

    async def _member_status(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> str:
        """@brief 安全读取并规范化 Telegram 成员状态 / Safely load and normalize a Telegram member status.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 小写状态；失败时为空字符串 / Lowercase status, or an empty string on failure.
        """

        try:
            member = await self._bot.get_chat_member(
                chat_id=town.group_id,
                user_id=actor.user_id,
            )
        except TelegramError:
            return ""
        raw_status = getattr(member, "status", "")
        """@brief PTB 枚举或字符串状态 / PTB enum or string status."""
        value = getattr(raw_status, "value", raw_status)
        """@brief 枚举解包后的状态值 / Unwrapped enum status value."""
        return str(value).casefold()


class TownTelegramCommandHandler:
    """@brief 将 `/town` 子命令映射为显式小镇范围与 durable 回包 / Map `/town` subcommands to explicit town scopes and durable replies."""

    def __init__(
        self,
        *,
        towns: TownService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入小镇服务与可靠回包能力 / Inject the town service and reliable reply capability.

        @param towns 群组小镇应用服务 / Group-town application service.
        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        """

        self._towns = towns
        """@brief 群组小镇应用服务 / Group-town application service."""
        self._outbound = outbound
        """@brief durable standalone outbox 能力 / Durable standalone-outbox capability."""

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回小镇命令所有权 / Return town command ownership.

        @return 仅 `/town` / Only `/town`.
        """

        return frozenset({"town"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行小镇子命令并写入确定性回包 / Execute a town subcommand and enqueue a deterministic reply.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析命令 envelope / Parsed command envelope.
        @return None / None.
        """

        if command.chat_type not in {"group", "supergroup"}:
            text = _GROUP_ONLY_TEXT
        else:
            text = await self._town_text(update, command)
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _town_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 解析 `/town` 的所有子命令 / Parse every `/town` subcommand.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析群组命令 / Parsed group command.
        @return 用户可见确定性文本 / User-visible deterministic text.
        """

        town = TownScope(command.chat_id)
        """@brief Telegram 群组到小镇范围的显式映射 / Explicit mapping from Telegram group to town scope."""
        actor = PersonalScope(command.user_id)
        """@brief Telegram 发送者到个人范围的显式映射 / Explicit mapping from Telegram sender to personal scope."""
        parts = command.argument_text.split(maxsplit=3)
        """@brief 至多四段的子命令参数 / Subcommand arguments split into at most four parts."""
        if not parts:
            result = await self._towns.ensure_town(
                EnsureTown(
                    town=town,
                    title=_default_town_title(town),
                    created_at=update.received_at,
                    idempotency_key=_key(update, command, "ensure"),
                )
            )
            return _town_result_text(result)

        action = parts[0].casefold()
        """@brief 规范化子命令名称 / Normalized subcommand name."""
        if action in {"overview", "info", "看"}:
            if len(parts) != 1:
                return _usage_text()
            return _town_result_text(await self._towns.overview(town))
        if action in {"project", "项目"}:
            return await self._project_text(update, command, town, actor)
        if action in {"contribute", "donate", "贡献"}:
            return await self._contribution_text(update, command, town, actor)
        if action in {"complete", "build", "建成"}:
            return await self._completion_text(update, command, town, actor)
        return _usage_text()

    async def _project_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        town: TownScope,
        actor: PersonalScope,
    ) -> str:
        """@brief 解析并提交一个项目提议 / Parse and submit one project proposal.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析群组命令 / Parsed group command.
        @param town 显式群组小镇范围 / Explicit group-town scope.
        @param actor 显式个人范围 / Explicit personal scope.
        @return 用户可见结果 / User-visible result.
        """

        parts = command.argument_text.split(maxsplit=3)
        if len(parts) != 4:
            return (
                "用法：/town project <hall|workshop|garden|observatory> <金币> <项目名>"
            )
        kind = _PROJECT_KIND_ALIASES.get(parts[1].casefold())
        if kind is None:
            return "项目类型只能是 hall、workshop、garden 或 observatory。"
        amount = _positive_amount(parts[2])
        if isinstance(amount, str):
            return amount
        title = parts[3].strip()
        if not title:
            return "项目名称不能为空。"
        result = await self._towns.create_project(
            CreateTownProject(
                town=town,
                proposer=actor,
                project_id=uuid4(),
                kind=kind,
                title=title,
                required_amount=amount,
                created_at=update.received_at,
                idempotency_key=_key(update, command, "project"),
            )
        )
        return _town_result_text(result)

    async def _contribution_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        town: TownScope,
        actor: PersonalScope,
    ) -> str:
        """@brief 解析并提交一笔 Free 金币贡献 / Parse and submit one Free-token contribution.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析群组命令 / Parsed group command.
        @param town 显式群组小镇范围 / Explicit group-town scope.
        @param actor 显式个人范围 / Explicit personal scope.
        @return 用户可见结果 / User-visible result.
        """

        parts = command.argument_text.split()
        if len(parts) not in {2, 3}:
            return "用法：/town contribute <免费金币> [项目ID]"
        amount = _positive_amount(parts[1])
        if isinstance(amount, str):
            return amount
        project_id = _optional_uuid(parts[2]) if len(parts) == 3 else None
        if len(parts) == 3 and project_id is None:
            return "项目 ID 必须是有效 UUID。"
        result = await self._towns.contribute(
            ContributeToTown(
                town=town,
                contributor=actor,
                contribution_id=uuid4(),
                amount=amount,
                requested_at=update.received_at,
                idempotency_key=_key(update, command, "contribute"),
                project_id=project_id,
            )
        )
        return _town_result_text(result)

    async def _completion_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        town: TownScope,
        actor: PersonalScope,
    ) -> str:
        """@brief 解析并结算一个足额项目 / Parse and settle one fully funded project.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析群组命令 / Parsed group command.
        @param town 显式群组小镇范围 / Explicit group-town scope.
        @param actor 显式个人范围 / Explicit personal scope.
        @return 用户可见结果 / User-visible result.
        """

        parts = command.argument_text.split()
        if len(parts) != 2:
            return "用法：/town complete <项目ID>"
        project_id = _optional_uuid(parts[1])
        if project_id is None:
            return "项目 ID 必须是有效 UUID。"
        result = await self._towns.complete_project(
            CompleteTownProject(
                town=town,
                operator=actor,
                project_id=project_id,
                completed_at=update.received_at,
                idempotency_key=_key(update, command, "complete"),
            )
        )
        return _town_result_text(result)


def _default_town_title(town: TownScope) -> str:
    """@brief 为未命名群组生成稳定初始小镇名 / Generate a stable initial town title for an unnamed group.

    @param town 群组小镇范围 / Group-town scope.
    @return 不依赖易变 chat title 的初始名称 / Initial title independent of mutable chat titles.
    """

    return f"雾萌小镇 #{abs(town.group_id)}"


def _key(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
    action: str,
) -> str:
    """@brief 从 durable Update 生成小镇操作幂等键 / Build a town-operation idempotency key from a durable update.

    @param update durable 来源 Update / Durable source Update.
    @param command 已解析命令 envelope / Parsed command envelope.
    @param action 稳定子操作名称 / Stable sub-operation name.
    @return 受长度约束的业务幂等键 / Length-bounded business idempotency key.
    """

    return f"telegram:town:{action}:{int(update.update_id)}:{command.user_id}"


def _positive_amount(raw_amount: str) -> TokenAmount | str:
    """@brief 解析严格正数的 Free 金币 / Parse strictly positive Free tokens.

    @param raw_amount 原始金额文本 / Raw amount text.
    @return 金额值对象，或用户可见错误 / Amount value object, or user-visible error.
    """

    try:
        return TokenAmount(int(raw_amount))
    except TypeError, ValueError:
        return "金币数量必须是正整数。"


def _optional_uuid(raw_value: str) -> UUID | None:
    """@brief 解析可选 UUID / Parse an optional UUID.

    @param raw_value 原始 UUID 文本 / Raw UUID text.
    @return UUID，非法时为 None / UUID, or None when invalid.
    """

    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _town_result_text(result: TownResult) -> str:
    """@brief 将 typed 小镇结果渲染为成员可见文本 / Render a typed town result as member-visible text.

    @param result 小镇应用结果 / Town application result.
    @return 用户可见文本 / User-visible text.
    """

    if result.code is TownCode.NOT_REGISTERED:
        return "请先私聊 Bot 使用 /me 注册账户，再向小镇贡献金币。"
    if result.code is TownCode.NOT_FOUND:
        return "这座小镇尚未建立；在群里直接发送 /town 即可创建。"
    if result.code is TownCode.FORBIDDEN:
        return "这项小镇操作需要当前群管理员权限；贡献则需要有效群成员身份。"
    if result.code is TownCode.INSUFFICIENT_FUNDS:
        return "你的 Free 金币不足；小镇贡献不会动用历史 Paid 余额。"
    if result.code is TownCode.PROJECT_UNAVAILABLE:
        return "该项目不存在、已满额或尚未达到可建成状态。"
    if result.code is TownCode.CONFLICT:
        return "小镇状态刚刚变化，或该请求与已有回执冲突；请刷新后重试。"
    if result.town is None:
        return "小镇操作已记录，但快照暂不可用。"
    prefix = "已回放同一请求的结果。\n" if result.replayed else ""
    action_line = _action_line(result)
    overview = _town_overview_text(result.town)
    return (
        f"{prefix}{action_line}\n{overview}" if action_line else f"{prefix}{overview}"
    )


def _action_line(result: TownResult) -> str:
    """@brief 为成功结果提取简洁动作描述 / Extract a concise action description for a successful result.

    @param result 成功的小镇结果 / Successful town result.
    @return 可选动作描述 / Optional action description.
    """

    if result.contribution is not None:
        target = (
            f"，定向项目 {result.contribution.project_id}"
            if result.contribution.project_id is not None
            else ""
        )
        return f"已贡献 {result.contribution.amount.value} 枚 Free 金币到群组金库{target}。"
    if result.project is not None:
        if result.project.status is TownProjectStatus.COMPLETED:
            return f"项目「{result.project.title}」已建成，繁荣度提升喵！"
        return (
            f"已提议项目「{result.project.title}」，ID：{result.project.project_id}。"
        )
    return "群组小镇已就绪。"


def _town_overview_text(town: Town) -> str:
    """@brief 渲染群组小镇概览 / Render a group-town overview.

    @param town 小镇聚合快照 / Town aggregate snapshot.
    @return 多行成员可见概览 / Multi-line member-visible overview.
    """

    treasury = town.treasury
    lines = [
        f"🏘 {town.title}",
        f"繁荣度：{town.prosperity}",
        (
            f"金库：{treasury.balance} Free（可自由分配 {treasury.available_balance}，"
            f"项目保留 {treasury.reserved}）"
        ),
        f"累计贡献：{treasury.lifetime_contributed} / {treasury.contribution_count} 笔",
    ]
    if not town.projects:
        lines.append("尚无项目：管理员可用 /town project <类型> <金币> <名称> 提议。")
        return "\n".join(lines)
    lines.append("项目：")
    for project in town.projects[-5:]:
        lines.append(
            "- "
            f"{_PROJECT_KIND_TEXT[project.kind]}「{project.title}」"
            f" {project.funded_amount}/{project.required_amount.value}"
            f" · {_PROJECT_STATUS_TEXT[project.status]}"
            f" · {project.project_id}"
        )
    return "\n".join(lines)


def _usage_text() -> str:
    """@brief 返回 `/town` 固定用法文本 / Return fixed `/town` usage text.

    @return 用户可见用法 / User-visible usage.
    """

    return (
        "群组小镇用法：\n"
        "/town — 创建或查看本群唯一小镇\n"
        "/town overview\n"
        "/town project <hall|workshop|garden|observatory> <金币> <项目名>\n"
        "/town contribute <免费金币> [项目ID]\n"
        "/town complete <项目ID>"
    )


__all__ = ["TelegramTownAuthorization", "TownTelegramCommandHandler"]
