"""@brief 基础经济用例与 durable Telegram handler 测试 / Tests for basic economy use cases and the durable Telegram handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import cast

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.economy.common import AccountLookup, EconomyCode
from fogmoe_bot.application.economy.community import (
    CommunityOperations,
    GiftCommand,
    GiftResult,
    LeaderboardCommand,
    LeaderboardResult,
)
from fogmoe_bot.application.economy.referral import ReferralOperations
from fogmoe_bot.application.economy.rewards import (
    LotteryCommand,
    LotteryResult,
    RewardOperations,
)
from fogmoe_bot.application.economy.service import EconomyService
from fogmoe_bot.application.economy.web_password import WebPasswordOperations
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)
from fogmoe_bot.presentation.telegram.economy_basic_handlers import (
    EconomyBasicTelegramCommandHandler,
)


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定时刻 / Fixed instant."""


class RecordingOperations:
    """@brief 记录基础经济 commands 的窄替身 / Narrow double recording basic economy commands."""

    def __init__(self) -> None:
        """@brief 初始化默认成功结果 / Initialize default successful results."""

        self.lottery_commands: list[LotteryCommand] = []
        """@brief 抽奖 commands / Lottery commands."""
        self.gift_commands: list[GiftCommand] = []
        """@brief 赠送 commands / Gift commands."""

    async def claim_lottery(self, command: LotteryCommand) -> LotteryResult:
        """@brief 记录抽奖 / Record a lottery claim.

        @param command lottery command / Lottery command.
        @return 成功结果 / Successful result.
        """

        self.lottery_commands.append(command)
        return LotteryResult(EconomyCode.SUCCESS, prize=command.prize)

    async def give(self, command: GiftCommand) -> GiftResult:
        """@brief 记录赠送 / Record a gift.

        @param command gift command / Gift command.
        @return 成功结果 / Successful result.
        """

        self.gift_commands.append(command)
        return GiftResult(
            EconomyCode.SUCCESS,
            target_name=command.target_name,
            amount=command.amount,
            fee=command.fee,
            available=100,
        )

    async def leaderboard(self, command: LeaderboardCommand) -> LeaderboardResult:
        """@brief 返回空排行榜 / Return an empty leaderboard.

        @param command 未使用命令 / Unused command.
        @return 空快照 / Empty snapshot.
        """

        del command
        return LeaderboardResult(EconomyCode.SUCCESS)


class RecordingOutbound:
    """@brief 记录 standalone responses / Record standalone responses."""

    def __init__(self) -> None:
        """@brief 初始化空记录 / Initialize an empty recording."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief responses / Responses."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录 response / Record a response.

        @param command outbound command / Outbound command.
        @return None / None.
        """

        self.commands.append(command)


def _service(operations: RecordingOperations) -> EconomyService:
    """@brief 将窄测试替身注入 service / Inject the narrow test double into the service.

    @param operations recording operations / Recording operations.
    @return Economy service / Economy service.
    """

    unused = object()
    """@brief 本测试不会触达的能力占位 / Capability placeholder unused by this test."""
    return EconomyService(
        accounts=cast(AccountLookup, unused),
        rewards=cast(RewardOperations, operations),
        community=cast(CommunityOperations, operations),
        referrals=cast(ReferralOperations, unused),
        web_passwords=cast(WebPasswordOperations, unused),
    )


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable Update / Build a durable Update.

    @param update_id Update ID / Update identifier.
    @return pending Update / Pending Update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(name: str, *arguments: str) -> ParsedTelegramCommand:
    """@brief 构造 parsed command / Build a parsed command.

    @param name command name / Command name.
    @param arguments command arguments / Command arguments.
    @return parsed envelope / Parsed envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=42,
        chat_id=-100,
        message_id=9,
        message_thread_id=7,
        username="klee",
        argument_text=" ".join(arguments),
        arguments=arguments,
    )


def test_service_derives_fee_cooldown_and_idempotency_command() -> None:
    """@brief service 在 adapter 外计算稳定规则 / The service derives stable rules outside the adapter."""

    operations = RecordingOperations()
    service = _service(operations)

    lottery = asyncio.run(
        service.claim_lottery(
            42,
            claimed_at=NOW,
            idempotency_key="telegram:lottery:1:42",
            prize=7,
        )
    )
    gift = asyncio.run(
        service.give(
            42,
            "@alice",
            10,
            business_date=date(2030, 1, 2),
            idempotency_key="telegram:gift:2:42",
        )
    )

    assert lottery.prize == 7
    assert operations.lottery_commands[0].cooldown.total_seconds() == 86_400
    assert gift.code is EconomyCode.SUCCESS
    assert operations.gift_commands[0].target_name == "alice"
    assert operations.gift_commands[0].fee == 2
    assert operations.gift_commands[0].daily_limit == 5


def test_handler_executes_gift_then_writes_deterministic_response() -> None:
    """@brief handler 调用 typed service 后只写 durable response / The handler calls the typed service and only writes a durable response."""

    operations = RecordingOperations()
    outbound = RecordingOutbound()
    handler = EconomyBasicTelegramCommandHandler(
        economy=_service(operations),
        outbound=outbound,
    )

    asyncio.run(handler.handle(_update(20), _command("give", "alice", "10")))

    assert operations.gift_commands[0].idempotency_key == "telegram:coin-gift:20:42"
    assert len(outbound.commands) == 1
    response = outbound.commands[0]
    assert response.idempotency_key == "update:20:command:give:response"
    assert (
        response.payload["text"] == "成功赠送 10 枚硬币给用户 alice，手续费 2 枚硬币。"
    )


def test_invalid_gift_never_reaches_business_port() -> None:
    """@brief 语法错误只生成 deterministic response / A syntax error only produces a deterministic response."""

    operations = RecordingOperations()
    outbound = RecordingOutbound()
    handler = EconomyBasicTelegramCommandHandler(
        economy=_service(operations),
        outbound=outbound,
    )

    asyncio.run(handler.handle(_update(21), _command("give", "alice", "zero")))

    assert operations.gift_commands == []
    assert outbound.commands[0].payload["text"] == "赠送数量必须为正整数！"
