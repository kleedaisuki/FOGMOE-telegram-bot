"""@brief 基础经济用例与 durable Telegram handler 测试 / Tests for basic economy use cases and the durable Telegram handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import cast

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.economy.check_in import (
    CheckInCommand,
    CheckInOperations,
    CheckInResult,
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
from fogmoe_bot.application.economy.lottery import (
    ClaimLotteryCommand,
    LotteryAlreadyClaimedResult,
    LotteryClaimTransaction,
    LotteryGrantedResult,
    LotteryNotRegisteredResult,
    LotteryResult,
)
from fogmoe_bot.application.economy.service import EconomyService
from fogmoe_bot.application.economy.web_password import WebPasswordOperations
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.economy.identity import EconomyAccountId
from fogmoe_bot.domain.economy.lottery import (
    PRIZE_BANDS,
    PRIZE_BAND_WEIGHTS,
    LotteryClaimInstant,
    LotteryPrize,
    LotteryPrizeBand,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)
from fogmoe_bot.presentation.telegram.economy_basic_handlers import (
    EconomyBasicTelegramCommandHandler,
)

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定时刻 / Fixed instant."""


class DeterministicLotteryRandomness:
    """@brief 固定返回七枚金币并记录消费次数的随机替身 /
    Randomness double returning seven tokens and recording consumption.
    """

    def __init__(self) -> None:
        """@brief 初始化零次随机消费 / Initialize with zero random consumptions."""

        self.band_draws = 0
        """@brief 档位抽取次数 / Number of band draws."""
        self.integer_draws = 0
        """@brief 整数抽取次数 / Number of integer draws."""

    def choose_prize_band(
        self,
        bands: tuple[LotteryPrizeBand, ...],
        weights: tuple[float, ...],
    ) -> LotteryPrizeBand:
        """@brief 验证领域策略参数并固定选择 medium / Validate policy inputs and select medium.

        @param bands 领域给出的档位 / Bands supplied by the domain.
        @param weights 领域给出的权重 / Weights supplied by the domain.
        @return medium 档位 / Medium band.
        """

        assert bands == PRIZE_BANDS
        assert weights == PRIZE_BAND_WEIGHTS
        self.band_draws += 1
        return LotteryPrizeBand.MEDIUM

    def integer_between(self, lower: int, upper: int) -> int:
        """@brief 验证 medium 边界并固定返回七 / Validate medium bounds and return seven.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 七 / Seven.
        """

        assert (lower, upper) == (5, 10)
        self.integer_draws += 1
        return 7


class RecordingOperations:
    """@brief 记录基础经济 commands 的窄替身 / Narrow double recording basic economy commands."""

    def __init__(self) -> None:
        """@brief 初始化默认成功结果 / Initialize default successful results."""

        self.lottery_commands: list[ClaimLotteryCommand] = []
        """@brief 抽奖 commands / Lottery commands."""
        self.lottery_result: LotteryResult | None = None
        """@brief 可选的预设抽奖结果 / Optional configured lottery result."""
        self.check_in_commands: list[CheckInCommand] = []
        """@brief 签到 commands / Check-in commands."""
        self.gift_commands: list[GiftCommand] = []
        """@brief 赠送 commands / Gift commands."""

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 记录签到 / Record a check-in.

        @param command 签到命令 / Check-in command.
        @return 固定成功结果 / Fixed successful result.
        """

        self.check_in_commands.append(command)
        return CheckInResult(
            code=EconomyCode.SUCCESS,
            consecutive_days=6,
            reward=2,
        )

    async def claim_lottery(self, command: ClaimLotteryCommand) -> LotteryResult:
        """@brief 记录抽奖 / Record a lottery claim.

        @param command lottery command / Lottery command.
        @return 成功结果 / Successful result.
        """

        self.lottery_commands.append(command)
        if self.lottery_result is not None:
            return self.lottery_result
        return LotteryGrantedResult(
            prize=command.proposed_prize,
            next_eligible_at=command.claimed_at.after_daily_cooldown(),
        )

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


def _service(
    operations: RecordingOperations,
    randomness: DeterministicLotteryRandomness | None = None,
) -> EconomyService:
    """@brief 将窄测试替身注入 service / Inject the narrow test double into the service.

    @param operations recording operations / Recording operations.
    @param randomness 可选固定随机替身 / Optional deterministic randomness double.
    @return Economy service / Economy service.
    """

    unused = object()
    """@brief 本测试不会触达的能力占位 / Capability placeholder unused by this test."""
    return EconomyService(
        accounts=cast(AccountLookup, unused),
        check_ins=cast(CheckInOperations, operations),
        lotteries=cast(LotteryClaimTransaction, operations),
        lottery_randomness=randomness or DeterministicLotteryRandomness(),
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


def test_service_builds_typed_lottery_and_gift_commands() -> None:
    """@brief service 只编排类型化领域值与应用消息 / The service only orchestrates typed domain values and application messages."""

    operations = RecordingOperations()
    service = _service(operations)

    lottery = asyncio.run(
        service.claim_lottery(
            42,
            claimed_at=NOW,
            idempotency_key="telegram:lottery:1:42",
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

    assert isinstance(lottery, LotteryGrantedResult)
    assert int(lottery.prize) == 7
    lottery_command = operations.lottery_commands[0]
    assert lottery_command.account_id == EconomyAccountId(42)
    assert lottery_command.claimed_at == LotteryClaimInstant(NOW)
    assert int(lottery_command.proposed_prize) == 7
    assert not hasattr(lottery_command, "cooldown")
    assert gift.code is EconomyCode.SUCCESS
    assert operations.gift_commands[0].target_name == "alice"
    assert operations.gift_commands[0].fee == 2
    assert operations.gift_commands[0].daily_limit == 5


def test_service_consumes_randomness_before_every_transaction_result() -> None:
    """@brief 未注册、冷却与重放调用都先消费候选奖励 /
    Missing-account, cooldown, and replay calls all consume a candidate prize first.

    @return None / None.
    """

    operations = RecordingOperations()
    randomness = DeterministicLotteryRandomness()
    service = _service(operations, randomness)
    next_eligible_at = LotteryClaimInstant(NOW + timedelta(hours=24))
    results: tuple[LotteryResult, ...] = (
        LotteryNotRegisteredResult(),
        LotteryAlreadyClaimedResult(next_eligible_at=next_eligible_at),
        LotteryGrantedResult(
            prize=LotteryPrize(7),
            next_eligible_at=next_eligible_at,
            replayed=True,
        ),
    )
    for ordinal, configured in enumerate(results, start=1):
        operations.lottery_result = configured
        actual = asyncio.run(
            service.claim_lottery(
                42,
                claimed_at=NOW,
                idempotency_key=f"telegram:lottery:{ordinal}:42",
            )
        )
        assert actual is configured

    assert randomness.band_draws == 3
    assert randomness.integer_draws == 3
    assert len(operations.lottery_commands) == 3


def test_service_orchestrates_a_validated_check_in_message() -> None:
    """@brief 应用服务只把已验证命令交给签到事务端口 /
    The application service only passes a validated command to the check-in transaction port.

    @return None / None.
    """

    operations = RecordingOperations()
    service = _service(operations)
    command = CheckInCommand(
        account_id=EconomyAccountId(42),
        day=date(2030, 1, 2),
        idempotency_key="telegram:checkin:1:42",
    )

    result = asyncio.run(service.check_in(command))

    assert result == CheckInResult(
        code=EconomyCode.SUCCESS,
        consecutive_days=6,
        reward=2,
    )
    assert operations.check_in_commands == [command]


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
