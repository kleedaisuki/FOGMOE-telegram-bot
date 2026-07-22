"""@brief Durable Telegram 可验证随机活动命令测试 / Tests for durable Telegram verifiable-chance commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fogmoe_bot.application.chance.models import PrivateCommittedChanceRound
from fogmoe_bot.application.chance.service import ChanceService, ServerSeedSource
from fogmoe_bot.application.chance.workflow import ChanceWorkflow
from fogmoe_bot.application.chance.workflow_models import (
    BindAndSettleChanceRound,
    ChanceRoundStatus,
    ChanceRoundView,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
    LookupChanceRound,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.chance.fairness import ServerSeed
from fogmoe_bot.domain.chance.money import FreeTokenStake
from fogmoe_bot.domain.chance.scope import GroupRoundScope, PersonalRoundScope
from fogmoe_bot.domain.conversation.identity import ConversationId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.chance_handlers import (
    ChanceTelegramCommandHandler,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 测试固定接收时刻 / Fixed receipt time for tests."""


class _FixedSeeds:
    """@brief 为命令测试提供稳定服务器种子 / Provide stable server seed for command tests."""

    def next_server_seed(self) -> ServerSeed:
        """@brief 返回固定服务器种子 / Return fixed server seed.

        @return 固定服务器种子 / Fixed server seed.
        """

        return ServerSeed(b"h" * 32)


class _Workflow:
    """@brief 记录 Telegram 适配器命令并生成有效视图 / Record Telegram-adapter commands and generate valid views."""

    def __init__(self) -> None:
        """@brief 初始化记录器与纯数学服务 / Initialize recorder and pure mathematical service."""

        self.commit_commands: list[CommitDurableChanceRound] = []
        """@brief 收到的承诺命令 / Received commitment commands."""
        self.bind_commands: list[BindAndSettleChanceRound] = []
        """@brief 收到的绑定结算命令 / Received bind-and-settle commands."""
        self.lookup_commands: list[LookupChanceRound] = []
        """@brief 收到的查询命令 / Received lookup commands."""
        self.private_round: PrivateCommittedChanceRound | None = None
        """@brief 最近私有承诺态 / Most recent private committed state."""
        self.next_code: ChanceWorkflowCode | None = None
        """@brief 可选的下一次工作流错误 / Optional next workflow error."""
        self._chance = ChanceService(cast(ServerSeedSource, _FixedSeeds()))

    async def commit(
        self,
        command: CommitDurableChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 记录并生成公开承诺视图 / Record and generate public commitment view.

        @param command 耐久承诺命令 / Durable commitment command.
        @return 公开承诺或预设错误结果 / Public commitment or preset error result.
        """

        self.commit_commands.append(command)
        if self.next_code is not None:
            code = self.next_code
            self.next_code = None
            return ChanceWorkflowResult(code)
        self.private_round = self._chance.commit(command.round)
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                self.private_round.committed_round,
                ChanceRoundStatus.COMMITTED,
            ),
        )

    async def bind_and_settle(
        self,
        command: BindAndSettleChanceRound,
    ) -> ChanceWorkflowResult:
        """@brief 记录并生成有效公平性结算 / Record and generate valid fairness settlement.

        @param command 绑定和结算命令 / Bind-and-settle command.
        @return 已结算视图或预设错误结果 / Settled view or preset error result.
        """

        self.bind_commands.append(command)
        if self.next_code is not None:
            code = self.next_code
            self.next_code = None
            return ChanceWorkflowResult(code)
        if self.private_round is None:
            return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
        prepared = self._chance.bind_client_seed(
            self.private_round, command.client_seed
        )
        settlement = prepared.settlement()
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                self.private_round.committed_round,
                ChanceRoundStatus.SETTLED,
                settlement,
            ),
        )

    async def lookup(self, command: LookupChanceRound) -> ChanceWorkflowResult:
        """@brief 记录并返回当前安全视图 / Record and return current safe view.

        @param command 查询命令 / Lookup command.
        @return 当前承诺视图或预设错误结果 / Current commitment view or preset error result.
        """

        self.lookup_commands.append(command)
        if self.next_code is not None:
            code = self.next_code
            self.next_code = None
            return ChanceWorkflowResult(code)
        if self.private_round is None:
            return ChanceWorkflowResult(ChanceWorkflowCode.NOT_FOUND)
        return ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(
                self.private_round.committed_round,
                ChanceRoundStatus.COMMITTED,
            ),
        )


class _Outbound:
    """@brief 记录 durable 命令回复 / Record durable command replies."""

    def __init__(self) -> None:
        """@brief 初始化空回复记录 / Initialize empty reply recording."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 已入队回复 / Enqueued replies."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一个 outbox 命令 / Record one outbox command.

        @param command outbox 命令 / Outbox command.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable 来源 Update / Build durable source Update.

    @param update_id Update 标识 / Update identifier.
    @return 待处理 durable Update / Pending durable Update.
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
    chat_id: int | None = None,
    thread_id: int | None = None,
) -> ParsedTelegramCommand:
    """@brief 构造已解析 Telegram 命令 / Build parsed Telegram command.

    @param name 命令名 / Command name.
    @param argument_text 原始参数文本 / Raw argument text.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param chat_id 可选 chat 标识 / Optional chat identity.
    @param thread_id 可选 topic 标识 / Optional topic identity.
    @return 已解析命令 envelope / Parsed command envelope.
    """

    resolved_chat_id = (
        chat_id if chat_id is not None else (42 if chat_type == "private" else -100_42)
    )
    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=42,
        chat_id=resolved_chat_id,
        message_id=9,
        message_thread_id=thread_id,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def _handler() -> tuple[ChanceTelegramCommandHandler, _Workflow, _Outbound]:
    """@brief 创建隔离 handler、工作流替身与 outbox / Create isolated handler, workflow double, and outbox.

    @return handler、工作流替身和 outbox / Handler, workflow double, and outbox.
    """

    workflow = _Workflow()
    outbound = _Outbound()
    return (
        ChanceTelegramCommandHandler(
            workflow=cast(ChanceWorkflow, workflow),
            outbound=outbound,
        ),
        workflow,
        outbound,
    )


def test_chance_commit_uses_free_stake_deterministic_uuid_and_source_idempotency() -> (
    None
):
    """@brief `/chance` 只传 free stake、确定性 UUID 和来源幂等键 / `/chance` passes only free stake, deterministic UUID, and source idempotency key."""

    async def scenario() -> None:
        """@brief 执行私聊承诺命令 / Execute private commitment command."""

        handler, workflow, outbound = _handler()
        update = _update(70)
        command = _command("chance", "big 10")

        await handler.handle(update, command)

        durable = workflow.commit_commands[0]
        assert durable.actor_id == 42
        assert durable.idempotency_key == "telegram:chance:commit:70:42:42:9"
        assert isinstance(durable.round.round_id, UUID)
        assert durable.round.nonce == 70
        assert durable.round.scope == PersonalRoundScope(42)
        assert durable.round.stake == FreeTokenStake(10)
        assert durable.round.rule_code == "big"
        assert (
            outbound.commands[0].idempotency_key == "update:70:command:chance:response"
        )
        assert outbound.commands[0].created_at == NOW
        text = str(outbound.commands[0].payload["text"])
        assert "Commitment" in text
        assert "EV" in text and "< 0" in text
        assert "Paid tokens）不可参与" in text

        await handler.handle(update, command)
        assert workflow.commit_commands[1].round.round_id == durable.round.round_id

    asyncio.run(scenario())


def test_chance_exposes_high_variance_rules_as_canonical_negative_ev_quotes() -> None:
    """@brief Telegram 可使用围骰和指定围骰，且仍只暴露负 EV 报价 /
    Telegram exposes any/exact triples while retaining only negative-EV quotes.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 用中文别名创建高方差承诺，再检查稳定规则编码 / Commit a high-variance round through a Chinese alias and inspect its stable rule code.

        @return None / None.
        """

        handler, workflow, outbound = _handler()

        await handler.handle(_update(700), _command("chance", "豹子 1"))

        any_triple = workflow.commit_commands[-1].round
        assert any_triple.rule_code == "any-triple"
        any_triple_quote = any_triple.ruleset.quote(
            any_triple.rule_code,
            any_triple.stake,
        )
        assert (
            any_triple_quote.win_probability.numerator
            < any_triple_quote.win_probability.denominator
        )
        assert any_triple_quote.expected_net_change < 0
        any_triple_text = str(outbound.commands[-1].payload["text"])
        assert "EV" in any_triple_text and "< 0" in any_triple_text

        await handler.handle(_update(701), _command("chance", "triple-6 1"))

        exact_triple = workflow.commit_commands[-1].round
        assert exact_triple.rule_code == "triple-6"
        exact_triple_quote = exact_triple.ruleset.quote(
            exact_triple.rule_code,
            exact_triple.stake,
        )
        assert exact_triple_quote.win_probability.numerator == 1
        assert exact_triple_quote.win_probability.denominator == 216
        assert exact_triple_quote.expected_net_change < 0
        assert (
            exact_triple_quote.gross_payout.value > any_triple_quote.gross_payout.value
        )

    asyncio.run(scenario())


def test_chance_seed_in_supergroup_preserves_explicit_scope_and_renders_proof() -> None:
    """@brief `/chance_seed` 保留群组/话题范围并渲染完整公平证明 / `/chance_seed` preserves group/topic scope and renders full fairness proof."""

    async def scenario() -> None:
        """@brief 执行群组承诺和结算命令 / Execute group commitment and settlement commands."""

        handler, workflow, outbound = _handler()
        group_command = _command(
            "chance",
            "even 10",
            chat_type="supergroup",
            chat_id=-100_42,
            thread_id=17,
        )
        await handler.handle(_update(71), group_command)
        assert workflow.private_round is not None
        round_id = workflow.private_round.committed_round.round_id

        await handler.handle(
            _update(72),
            _command(
                "chance_seed",
                f"{round_id} Klee deterministic seed",
                chat_type="supergroup",
                chat_id=-100_42,
                thread_id=17,
            ),
        )

        bind = workflow.bind_commands[0]
        assert bind.round_id == round_id
        assert bind.scope == GroupRoundScope(-100_42, topic_id=17)
        assert bind.client_seed.value == "Klee deterministic seed"
        assert bind.idempotency_key == "telegram:chance:bind-and-settle:72:42:-10042:9"
        text = str(outbound.commands[-1].payload["text"])
        assert "公平性证明" in text
        assert "Server seed（已揭示）" in text
        assert "HMAC-SHA-256" in text
        assert "EV" in text and "< 0" in text

    asyncio.run(scenario())


def test_chance_show_hides_private_seed_before_settlement_and_renders_errors() -> None:
    """@brief `/chance_show` 在未结算时隐藏服务器种子，并展示工作流错误码 / `/chance_show` hides server seed before settlement and renders workflow error codes."""

    async def scenario() -> None:
        """@brief 执行查询和余额不足错误场景 / Execute lookup and insufficient-free-token error scenario."""

        handler, workflow, outbound = _handler()
        await handler.handle(_update(73), _command("chance", "small 10"))
        assert workflow.private_round is not None
        round_id = workflow.private_round.committed_round.round_id

        await handler.handle(
            _update(74),
            _command("chance_show", str(round_id)),
        )
        pending_text = str(outbound.commands[-1].payload["text"])
        assert "Commitment" in pending_text
        assert "Server seed（已揭示）" not in pending_text
        assert "EV" in pending_text and "< 0" in pending_text

        workflow.next_code = ChanceWorkflowCode.INSUFFICIENT_FREE_TOKENS
        await handler.handle(_update(75), _command("chance", "odd 10"))
        error_text = str(outbound.commands[-1].payload["text"])
        assert "免费金币不足" in error_text
        assert "错误码：insufficient_free_tokens" in error_text

        workflow.next_code = ChanceWorkflowCode.INSUFFICIENT_ACTIVITY_POT
        await handler.handle(_update(750), _command("chance", "triple-1 10"))
        pot_text = str(outbound.commands[-1].payload["text"])
        assert "活动奖池准备中" in pot_text
        assert "不会扣除免费金币" in pot_text
        assert "错误码：insufficient_activity_pot" in pot_text

    asyncio.run(scenario())


def test_invalid_or_unsupported_context_never_reaches_chance_workflow() -> None:
    """@brief 非法押注或不支持 chat 类型只回复，不调用工作流 / Invalid stake or unsupported chat type only replies and does not call workflow."""

    async def scenario() -> None:
        """@brief 执行参数和范围拒绝场景 / Execute argument and scope rejection scenario."""

        handler, workflow, outbound = _handler()
        await handler.handle(_update(76), _command("chance", "big paid"))
        assert workflow.commit_commands == []
        assert "付费金币不能参与" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(760), _command("chance", "unknown-rule 10"))
        assert workflow.commit_commands == []
        assert "高方差" in str(outbound.commands[-1].payload["text"])

        await handler.handle(
            _update(77),
            _command("chance", "big 10", chat_type="channel", chat_id=-100_42),
        )
        assert workflow.commit_commands == []
        assert "错误码：unsupported_scope" in str(outbound.commands[-1].payload["text"])

    asyncio.run(scenario())
