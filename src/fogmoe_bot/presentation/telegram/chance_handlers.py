"""@brief Durable Telegram 可验证随机活动命令 / Durable Telegram commands for verifiable chance activities."""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from types import MappingProxyType
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from fogmoe_bot.application.chance.models import CommitChanceRound
from fogmoe_bot.application.chance.workflow import ChanceWorkflow
from fogmoe_bot.application.chance.workflow_models import (
    BindAndSettleChanceRound,
    ChanceRoundStatus,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.domain.chance.examples import sicbo_like_ruleset
from fogmoe_bot.domain.chance.fairness import ClientSeed
from fogmoe_bot.domain.chance.money import FreeTokenStake
from fogmoe_bot.domain.chance.rules import ChanceRuleset
from fogmoe_bot.domain.chance.scope import (
    GroupRoundScope,
    PersonalRoundScope,
    RoundScope,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


_EXPOSED_RULES: Final[frozenset[str]] = frozenset(
    {
        "big",
        "small",
        "odd",
        "even",
        "any-triple",
        "triple-1",
        "triple-2",
        "triple-3",
        "triple-4",
        "triple-5",
        "triple-6",
    }
)
"""@brief Telegram 公开的骰宝风格规则 / Sic-Bo-like rules exposed through Telegram.

``any-triple`` 与 ``triple-1`` 至 ``triple-6`` 是低命中、高派彩的高方差（high
variance）选项；它们并不自带赔率，仍必须由冻结规则集的 ``quote`` 生成严格负期望报价。
``any-triple`` and ``triple-1`` through ``triple-6`` are low-hit, high-payout, high-variance
options. They carry no caller-supplied odds; a frozen ruleset's ``quote`` still produces their
strictly negative-EV quote.
"""

_RULE_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "大": "big",
        "小": "small",
        "单": "odd",
        "双": "even",
        "triple": "any-triple",
        "any_triple": "any-triple",
        "豹子": "any-triple",
        "围骰": "any-triple",
        "triple_1": "triple-1",
        "triple_2": "triple-2",
        "triple_3": "triple-3",
        "triple_4": "triple-4",
        "triple_5": "triple-5",
        "triple_6": "triple-6",
    }
)
"""@brief Telegram 输入别名到稳定规则编码 / Telegram input aliases to stable rule codes."""

_SUPPORTED_CHAT_TYPES: Final[frozenset[str]] = frozenset(
    {"private", "group", "supergroup"}
)
"""@brief 可显式映射到个人或群组范围的 Telegram chat 类型 / Telegram chat types mappable to explicit personal or group scopes."""


class ChanceTelegramCommandHandler:
    """@brief 将可验证随机活动命令映射到耐久工作流与 outbox / Map verifiable chance commands to durable workflow and outbox.

    本适配器只构造 ``FreeTokenStake``，从未接收或传递付费钱包（paid wallet）资产。
    This adapter constructs only ``FreeTokenStake`` and never receives or passes a paid-wallet
    asset.

    @param workflow 随机活动耐久工作流 / Durable chance-activity workflow.
    @param outbound durable standalone outbox 能力 / Durable standalone-outbox capability.
    @param ruleset 当前 Telegram 可用的冻结规则集 / Frozen ruleset available through Telegram.
    """

    def __init__(
        self,
        *,
        workflow: ChanceWorkflow,
        outbound: StandaloneOutboundCapability,
        ruleset: ChanceRuleset | None = None,
    ) -> None:
        """@brief 注入工作流、outbox 与规则集 / Inject workflow, outbox, and ruleset.

        @param workflow 随机活动耐久工作流 / Durable chance-activity workflow.
        @param outbound durable standalone outbox 能力 / Durable standalone-outbox capability.
        @param ruleset 可选替代规则集，默认骰宝风格示例 / Optional replacement ruleset; Sic-Bo-like example by default.
        @raise TypeError 规则集类型不匹配时抛出 / Raised when ruleset type does not match.
        @raise ValueError 规则集缺少 Telegram 已公开规则时抛出 /
            Raised when ruleset lacks a rule exposed through Telegram.
        """

        selected_ruleset = ruleset or sicbo_like_ruleset()
        if not isinstance(selected_ruleset, ChanceRuleset):
            raise TypeError("Chance Telegram handler requires ChanceRuleset")
        available_rules = frozenset(rule.code for rule in selected_ruleset.rules)
        missing_rules = _EXPOSED_RULES - available_rules
        if missing_rules:
            raise ValueError(
                "Chance Telegram ruleset lacks exposed rules: "
                + ", ".join(sorted(missing_rules))
            )
        self._workflow = workflow
        """@brief 耐久随机活动工作流 / Durable chance-activity workflow."""
        self._outbound = outbound
        """@brief durable standalone outbox 能力 / Durable standalone-outbox capability."""
        self._ruleset = selected_ruleset
        """@brief Telegram 暴露的冻结规则集 / Frozen ruleset exposed by Telegram."""

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回随机活动命令所有权 / Return chance-command ownership.

        @return chance/chance_seed/chance_show / chance/chance_seed/chance_show.
        """

        return frozenset({"chance", "chance_seed", "chance_show"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行随机活动命令并入队确定性回复 / Execute a chance command and enqueue deterministic reply.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析命令 envelope / Parsed command envelope.
        @return None / None.
        """

        scope = _scope_for(command)
        if isinstance(scope, str):
            text = scope
        elif command.command == "chance":
            text = await self._commit_text(update, command, scope)
        elif command.command == "chance_seed":
            text = await self._bind_and_settle_text(update, command, scope)
        elif command.command == "chance_show":
            text = await self._lookup_text(command, scope)
        else:
            raise ValueError("Chance handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _commit_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        scope: RoundScope,
    ) -> str:
        """@brief 创建公开承诺并渲染下一步 / Create public commitment and render next step.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析 `/chance` / Parsed `/chance`.
        @param scope 已解析个人或群组范围 / Resolved personal or group scope.
        @return 用户可见文本 / User-facing text.
        """

        parsed = _chance_arguments(command.arguments)
        if isinstance(parsed, str):
            return parsed
        rule_code, stake = parsed
        round_id = _source_round_id(update, command)
        result = await self._workflow.commit(
            CommitDurableChanceRound(
                actor_id=command.user_id,
                round=CommitChanceRound(
                    round_id=round_id,
                    scope=scope,
                    player_id=command.user_id,
                    ruleset=self._ruleset,
                    rule_code=rule_code,
                    stake=stake,
                    nonce=int(update.update_id),
                ),
                idempotency_key=_source_key(update, command, "commit"),
            )
        )
        return _commit_result_text(result)

    async def _bind_and_settle_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
        scope: RoundScope,
    ) -> str:
        """@brief 绑定客户端种子并请求原子结算 / Bind client seed and request atomic settlement.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析 `/chance_seed` / Parsed `/chance_seed`.
        @param scope 已解析个人或群组范围 / Resolved personal or group scope.
        @return 用户可见文本 / User-facing text.
        """

        parsed = _seed_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        round_id, client_seed = parsed
        result = await self._workflow.bind_and_settle(
            BindAndSettleChanceRound(
                round_id=round_id,
                actor_id=command.user_id,
                scope=scope,
                client_seed=client_seed,
                idempotency_key=_source_key(update, command, "bind-and-settle"),
            )
        )
        return _settlement_result_text(result)

    async def _lookup_text(
        self,
        command: ParsedTelegramCommand,
        scope: RoundScope,
    ) -> str:
        """@brief 查询安全轮次视图 / Look up a safe chance-round view.

        @param command 已解析 `/chance_show` / Parsed `/chance_show`.
        @param scope 已解析个人或群组范围 / Resolved personal or group scope.
        @return 用户可见文本 / User-facing text.
        """

        round_id = _show_arguments(command.arguments)
        if isinstance(round_id, str):
            return round_id
        result = await self._workflow.lookup(
            LookupChanceRound(
                round_id=round_id,
                actor_id=command.user_id,
                scope=scope,
            )
        )
        return _lookup_result_text(result)


def _scope_for(command: ParsedTelegramCommand) -> RoundScope | str:
    """@brief 从 Telegram 上下文构造显式活动范围 / Construct explicit activity scope from Telegram context.

    @param command 已解析命令 envelope / Parsed command envelope.
    @return 个人/群组范围，或用户可见范围错误 / Personal/group scope, or user-visible scope error.
    """

    chat_type = command.chat_type.casefold()
    if chat_type == "private":
        return PersonalRoundScope(command.user_id)
    if chat_type in {"group", "supergroup"}:
        return GroupRoundScope(command.chat_id, command.message_thread_id)
    return (
        "随机活动仅支持私聊、群组或超级群组上下文。\n"
        "（错误码：unsupported_scope）"
    )


def _chance_arguments(
    arguments: tuple[str, ...],
) -> tuple[str, FreeTokenStake] | str:
    """@brief 解析 `/chance <规则> <free_stake>` / Parse a chance rule and free stake.

    @param arguments 空白切分后的命令参数 / Whitespace-split command arguments.
    @return 规则与免费押注，或用户可见用法错误 / Rule and free stake, or user-visible usage error.
    """

    if len(arguments) != 2:
        return _chance_usage_text()
    requested_rule = arguments[0].casefold()
    rule_code = _RULE_ALIASES.get(requested_rule, requested_rule)
    if rule_code not in _EXPOSED_RULES:
        return _chance_usage_text()
    try:
        stake = FreeTokenStake(int(arguments[1]))
    except (TypeError, ValueError):
        return "免费金币押注必须是正整数；付费金币不能参与随机活动。"
    return rule_code, stake


def _chance_usage_text() -> str:
    """@brief 返回完整的可验证活动规则用法 / Return complete verifiable-activity rule usage.

    @return 用户可见的规则和押注说明 / User-visible rule and stake guidance.
    """

    return (
        "用法：/chance <规则> <免费金币押注>\n"
        "常规：big、small、odd、even（也可输入 大、小、单、双）\n"
        "高方差：any-triple（豹子/围骰）或 triple-1 至 triple-6"
    )


def _seed_arguments(argument_text: str) -> tuple[UUID, ClientSeed] | str:
    """@brief 解析 `/chance_seed <round_uuid> <client_seed>` / Parse round UUID and client seed.

    @param argument_text 原始命令参数文本 / Raw command argument text.
    @return 轮次 UUID 与客户端种子，或用户可见用法错误 / Round UUID and client seed, or user-visible usage error.
    """

    parts = argument_text.split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/chance_seed <轮次UUID> <客户端种子>"
    try:
        round_id = UUID(parts[0])
    except ValueError:
        return "轮次 UUID 格式无效。"
    try:
        client_seed = ClientSeed(parts[1])
    except (TypeError, ValueError):
        return "客户端种子需为 1–512 个 UTF-8 字节，且不能包含 NUL。"
    return round_id, client_seed


def _show_arguments(arguments: tuple[str, ...]) -> UUID | str:
    """@brief 解析 `/chance_show <round_uuid>` / Parse a round UUID for lookup.

    @param arguments 空白切分后的命令参数 / Whitespace-split command arguments.
    @return 轮次 UUID，或用户可见用法错误 / Round UUID, or user-visible usage error.
    """

    if len(arguments) != 1:
        return "用法：/chance_show <轮次UUID>"
    try:
        return UUID(arguments[0])
    except ValueError:
        return "轮次 UUID 格式无效。"


def _source_round_id(update: InboundUpdate, command: ParsedTelegramCommand) -> UUID:
    """@brief 从 Telegram 来源事件与持久接收时刻确定性派生轮次 UUID / Deterministically derive round UUID from Telegram source event and persisted receipt time.

    @param update durable 来源 Update / Durable source Update.
    @param command 已解析命令 envelope / Parsed command envelope.
    @return 稳定的 UUIDv5 轮次标识 / Stable UUIDv5 round identity.
    """

    return uuid5(
        NAMESPACE_URL,
        (
            "fogmoe:chance:round:"
            f"{int(update.update_id)}:{command.user_id}:{command.chat_id}:"
            f"{command.message_thread_id or 0}:{command.message_id}:"
            f"{update.received_at.isoformat()}"
        ),
    )


def _source_key(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
    operation: str,
) -> str:
    """@brief 构造来源 Update 绑定的工作流幂等键 / Construct source-Update-bound workflow idempotency key.

    @param update durable 来源 Update / Durable source Update.
    @param command 已解析命令 envelope / Parsed command envelope.
    @param operation 工作流操作名 / Workflow operation name.
    @return 稳定业务幂等键 / Stable business idempotency key.
    """

    return (
        f"telegram:chance:{operation}:{int(update.update_id)}:"
        f"{command.user_id}:{command.chat_id}:{command.message_id}"
    )


def _commit_result_text(result: ChanceWorkflowResult) -> str:
    """@brief 渲染承诺创建结果 / Render commitment-creation result.

    @param result 工作流结果 / Workflow result.
    @return 用户可见文本 / User-facing text.
    """

    if result.code is not ChanceWorkflowCode.SUCCESS or result.view is None:
        return _error_text(result.code)
    view = result.view
    quote = view.committed_round.quote
    replay = "\n（本次为同源幂等重放。）" if result.replayed else ""
    return (
        "🎲 可验证随机活动已承诺\n"
        f"轮次 UUID：{view.round_id}\n"
        f"范围：{_scope_text(view.scope)}\n"
        f"规则：{view.committed_round.rule_code}\n"
        f"免费金币押注：{view.committed_round.stake.value}\n"
        f"胜率：{_fraction_text(quote.win_probability)}\n"
        f"胜利总派彩：{quote.gross_payout.value} 免费金币\n"
        f"精确期望净变化（EV）：{_fraction_text(quote.expected_net_change)} < 0\n"
        f"配置庄家优势：{_fraction_text(quote.configured_house_edge)}\n"
        f"承诺 Commitment：{view.committed_round.commitment.hex_digest}\n"
        f"规则集指纹：{view.committed_round.ruleset_fingerprint}\n\n"
        "本活动只使用免费金币（Free tokens）；历史付费金币（Paid tokens）不可参与。\n"
        f"下一步：/chance_seed {view.round_id} <客户端种子>{replay}"
    )


def _settlement_result_text(result: ChanceWorkflowResult) -> str:
    """@brief 渲染绑定和结算结果 / Render bind-and-settle result.

    @param result 工作流结果 / Workflow result.
    @return 用户可见文本 / User-facing text.
    """

    if result.code is not ChanceWorkflowCode.SUCCESS or result.view is None:
        return _error_text(result.code)
    if result.view.status is not ChanceRoundStatus.SETTLED or result.view.settlement is None:
        return "随机活动状态尚未结算，请稍后用 /chance_show 查询。（错误码：invalid_state）"
    settlement = result.view.settlement
    proof = settlement.proof
    quote = settlement.round.quote
    result_line = (
        f"命中！贷记 {settlement.credited} 枚免费金币。"
        if settlement.won
        else "未命中，本轮没有派彩。"
    )
    replay = "\n（本次为同源幂等重放。）" if result.replayed else ""
    return (
        "🎲 可验证随机活动已结算\n"
        f"轮次 UUID：{settlement.round.round_id}\n"
        f"范围：{_scope_text(settlement.round.scope)}\n"
        f"结果：{settlement.outcome.code}\n"
        f"{result_line}\n"
        f"实现净变化：{settlement.net_change}\n"
        f"精确期望净变化（EV）：{_fraction_text(quote.expected_net_change)} < 0\n\n"
        "公平性证明（Provably Fair Proof）\n"
        f"Commitment：{proof.commitment.hex_digest}\n"
        f"Server seed（已揭示）：{proof.revealed_server_seed.reveal_hex()}\n"
        f"Client seed：{proof.client_seed.value}\n"
        f"Nonce：{proof.nonce}\n"
        f"Rejection attempt：{proof.sample.attempt}\n"
        f"Unbiased ticket：{proof.sample.ticket}\n"
        f"HMAC-SHA-256：{proof.sample.digest_hex}\n"
        f"规则集指纹：{settlement.round.ruleset_fingerprint}{replay}"
    )


def _lookup_result_text(result: ChanceWorkflowResult) -> str:
    """@brief 渲染查询结果 / Render lookup result.

    @param result 工作流结果 / Workflow result.
    @return 用户可见文本 / User-facing text.
    """

    if result.code is not ChanceWorkflowCode.SUCCESS or result.view is None:
        return _error_text(result.code)
    if result.view.status is ChanceRoundStatus.SETTLED:
        return _settlement_result_text(result)
    view = result.view
    quote = view.committed_round.quote
    return (
        "🎲 可验证随机活动待揭示\n"
        f"轮次 UUID：{view.round_id}\n"
        f"范围：{_scope_text(view.scope)}\n"
        f"规则：{view.committed_round.rule_code}\n"
        f"免费金币押注：{view.committed_round.stake.value}\n"
        f"承诺 Commitment：{view.committed_round.commitment.hex_digest}\n"
        f"规则集指纹：{view.committed_round.ruleset_fingerprint}\n"
        f"精确期望净变化（EV）：{_fraction_text(quote.expected_net_change)} < 0\n"
        f"请使用：/chance_seed {view.round_id} <客户端种子>"
    )


def _error_text(code: ChanceWorkflowCode) -> str:
    """@brief 将工作流错误代码映射为用户可见文本 / Map workflow error code to user-visible text.

    @param code 工作流错误代码 / Workflow error code.
    @return 用户可见错误文本 / User-visible error text.
    """

    messages = {
        ChanceWorkflowCode.NOT_FOUND: "找不到该随机活动轮次，或你无权查看它。",
        ChanceWorkflowCode.FORBIDDEN: "你不是该随机活动的允许操作人。",
        ChanceWorkflowCode.SCOPE_MISMATCH: "该轮次不属于当前私聊、群组或话题上下文。",
        ChanceWorkflowCode.ALREADY_SETTLED: "该轮次已经结算，不能再次扣款或揭示。",
        ChanceWorkflowCode.INSUFFICIENT_FREE_TOKENS: "免费金币不足；付费金币不能参与随机活动。",
        ChanceWorkflowCode.INSUFFICIENT_ACTIVITY_POT: "活动奖池准备中，请稍后再试；本次不会扣除免费金币。",
        ChanceWorkflowCode.CONFLICT: "该操作与已有状态或幂等请求冲突，请使用 /chance_show 查询。",
        ChanceWorkflowCode.SUCCESS: "随机活动返回了不完整的成功视图，请稍后重试。",
    }
    return f"{messages[code]}\n（错误码：{code.value}）"


def _scope_text(scope: RoundScope) -> str:
    """@brief 渲染显式个人或群组范围 / Render explicit personal or group scope.

    @param scope 个人或群组范围 / Personal or group scope.
    @return 用户可见范围文本 / User-visible scope text.
    """

    if isinstance(scope, PersonalRoundScope):
        return f"个人随机活动（用户 {scope.user_id}）"
    topic = f"，话题 {scope.topic_id}" if scope.topic_id is not None else ""
    return f"群组小镇（群 {scope.group_id}{topic}）"


def _fraction_text(value: Fraction) -> str:
    """@brief 将精确分数渲染为紧凑文本 / Render an exact fraction as compact text.

    @param value 精确有理数 / Exact rational number.
    @return 整数或分子/分母文本 / Integer or numerator/denominator text.
    """

    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"
