"""@brief Durable `/lottery` 与 `/give` 经济命令 / Durable `/lottery` and `/give` economy commands."""

from __future__ import annotations

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.community import (
    GiftResult,
    LeaderboardResult,
)
from fogmoe_bot.application.economy.rewards import LotteryResult
from fogmoe_bot.application.economy.service import EconomyService
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


class EconomyBasicTelegramCommandHandler:
    """@brief 将基础经济命令映射到 typed service 与 durable outbox / Map basic economy commands to a typed service and durable outbox."""

    def __init__(
        self,
        *,
        economy: EconomyService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入经济服务与 outbox / Inject the economy service and outbox.

        @param economy 原子经济用例 / Atomic economy use cases.
        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        """

        self._economy = economy
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回命令所有权 / Return command ownership.

        @return lottery/give / lottery/give.
        """

        return frozenset({"lottery", "give", "rich"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行业务命令并发布确定性回复 / Execute a business command and publish a deterministic reply.

        @param update durable source Update / Durable source Update.
        @param command parsed command / Parsed command.
        @return None / None.
        """

        if command.command == "lottery":
            lottery_result = await self._economy.claim_lottery(
                command.user_id,
                claimed_at=update.received_at,
                idempotency_key=(
                    f"telegram:lottery:{int(update.update_id)}:{command.user_id}"
                ),
            )
            text = _lottery_text(lottery_result)
        elif command.command == "give":
            parsed_gift = _gift_arguments(command.arguments)
            if isinstance(parsed_gift, str):
                text = parsed_gift
            else:
                target, amount = parsed_gift
                gift_result = await self._economy.give(
                    command.user_id,
                    target,
                    amount,
                    business_date=update.received_at.date(),
                    idempotency_key=(
                        f"telegram:coin-gift:{int(update.update_id)}:{command.user_id}"
                    ),
                )
                text = _gift_text(gift_result, amount=amount)
        elif command.command == "rich":
            leaderboard = await self._economy.leaderboard(
                command.user_id,
                idempotency_key=(
                    f"telegram:leaderboard:{int(update.update_id)}:{command.user_id}"
                ),
            )
            text = _leaderboard_text(leaderboard)
        else:
            raise ValueError("Economy basic handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)


def _gift_arguments(arguments: tuple[str, ...]) -> tuple[str, int] | str:
    """@brief 严格解析 `/give <name> <amount>` / Strictly parse `/give <name> <amount>`.

    @param arguments 命令参数 / Command arguments.
    @return 目标与金额，或用户可见错误 / Target and amount, or a user-visible error.
    """

    if len(arguments) != 2:
        return "用法：/give <用户名> <数量>\n严禁恶意刷硬币、出售，违规者将被封禁！"
    try:
        amount = int(arguments[1])
    except ValueError:
        return "赠送数量必须为正整数！"
    if amount <= 0:
        return "赠送数量必须为正整数！"
    return arguments[0], amount


def _lottery_text(result: LotteryResult) -> str:
    """@brief 渲染抽奖结果 / Render a lottery result.

    @param result typed result / Typed result.
    @return 用户文本 / User-facing text.
    """

    if result.code is EconomyCode.SUCCESS:
        return (
            f"恭喜！您赢得了 {result.prize} 枚硬币喵。\n"
            f"Congratulations! You have won {result.prize} coins. Meow!"
        )
    if result.code is EconomyCode.ALREADY_CLAIMED:
        return (
            "每24小时您只能参加一次抽奖喵。下次再来吧！\n"
            "You can only participate in the lottery once every 24 hours. Meow! Come back later!"
        )
    return (
        "请先使用 /me 命令获取个人信息。\nPlease register first using the /me command."
    )


def _gift_text(result: GiftResult, *, amount: int) -> str:
    """@brief 渲染赠送结果 / Render a gift result.

    @param result typed result / Typed result.
    @param amount 请求到账金额 / Requested credited amount.
    @return 用户文本 / User-facing text.
    """

    if result.code is EconomyCode.SUCCESS:
        fee = f"，手续费 {result.fee} 枚硬币" if result.fee else ""
        return f"成功赠送 {result.amount} 枚硬币给用户 {result.target_name}{fee}。"
    if result.code is EconomyCode.NOT_REGISTERED:
        return "请先使用 /me 命令注册个人信息。"
    if result.code is EconomyCode.NOT_FOUND:
        return f"未找到用户名为 '{result.target_name}' 的用户。"
    if result.code is EconomyCode.SELF_TRANSFER:
        return "不能给自己赠送硬币哦~"
    if result.code is EconomyCode.DAILY_LIMIT:
        return "您今天的赠送次数已达上限（5次），请明天再试。"
    required = amount + result.fee
    return f"您的硬币不足，当前硬币：{result.available}，需要：{required}"


def _leaderboard_text(result: LeaderboardResult) -> str:
    """@brief 渲染稳定排行榜 / Render a stable leaderboard.

    @param result typed snapshot / Typed snapshot.
    @return 用户文本 / User-facing text.
    """

    if result.code is EconomyCode.NOT_REGISTERED:
        return "请先使用 /me 命令注册个人信息。"
    if not result.entries:
        return "暂无数据"
    lines = ["富豪榜 Top 5", ""]
    lines.extend(
        f"{index}. {entry.name} - {entry.coins} 枚硬币"
        for index, entry in enumerate(result.entries, start=1)
    )
    return "\n".join(lines)


__all__ = ["EconomyBasicTelegramCommandHandler"]
