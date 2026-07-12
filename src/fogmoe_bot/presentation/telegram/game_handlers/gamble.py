"""@brief 多人奖池 Telegram 适配器 / Telegram adapter for multiplayer gamble pools."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from fogmoe_bot.application.games.gamble.models import (
    GambleCode,
    GambleSettlement,
    OpenGamble,
    PlaceGambleBet,
)
from fogmoe_bot.application.games.gamble.service import (
    GAMBLE_SERVICE_DATA_KEY,
    GambleService,
)
from fogmoe_bot.application.games.ports.gamble import GambleSettlementRenderer
from fogmoe_bot.domain.games import GambleSession, GameSessionId

from .common import TelegramContext, current_time, idempotency_key

_GAMBLE_DURATION = timedelta(minutes=5)
"""@brief 多人奖池旧持续时间 / Legacy multiplayer-pool duration."""


def _service(context: TelegramContext) -> GambleService:
    """@brief 读取多人奖池 capability / Read the multiplayer-pool capability."""

    value = context.application.bot_data.get(GAMBLE_SERVICE_DATA_KEY)
    if not isinstance(value, GambleService):
        raise RuntimeError("Gamble service was not assembled")
    return value


def _gamble_callback(session: GambleSession, amount: int) -> str:
    """@brief 编码奖池 callback / Encode a pool callback.

    @param session 奖池会话 / Pool session.
    @param amount 押注额 / Wager amount.
    @return 不超过 Bot API 限制的 callback / Callback within Bot API limits.
    """

    return f"gamble:{session.session_id.value.hex}:{amount}"


def _gamble_keyboard(session: GambleSession) -> InlineKeyboardMarkup:
    """@brief 构造奖池押注键盘 / Build the pool wager keyboard.

    @param session 奖池会话 / Pool session.
    @return Telegram keyboard / Telegram keyboard.
    """

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"押注 {amount} 金币",
                    callback_data=_gamble_callback(session, amount),
                )
                for amount in (5, 10, 20)
            ]
        ]
    )


def _gamble_text(session: GambleSession) -> str:
    """@brief 渲染活动奖池 / Render an active pool.

    @param session 奖池会话 / Pool session.
    @return 展示文本 / Display text.
    """

    participants = _render_gamble_bets(session, max_chars=3500)
    return (
        "赌博开始！请点击下面按钮选择押注金额。\n\n当前参与者：\n"
        f"{participants or '暂无'}\n\n开奖时间：5分钟"
    )


def _render_gamble_bets(session: GambleSession, *, max_chars: int) -> str:
    """@brief 在 Telegram 文本预算内渲染参与者 / Render participants within a Telegram text budget.

    @param session 奖池会话 / Pool session.
    @param max_chars 最大字符数 / Maximum characters.
    @return 有界参与详情 / Bounded participant details.
    """

    lines: list[str] = []
    used = 0
    for index, bet in enumerate(session.bets):
        line = f"@{bet.display_name} 押注 {bet.amount} 金币"
        separator = 1 if lines else 0
        if used + separator + len(line) > max_chars:
            lines.append(f"…另有 {len(session.bets) - index} 位参与者")
            break
        lines.append(line)
        used += separator + len(line)
    return "\n".join(lines)


async def gamble_command(update: Update, context: TelegramContext) -> None:
    """@brief 解析 /gamble 并开启 durable 奖池 / Parse /gamble and open a durable pool.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return
    placeholder = await message.reply_text("正在开启赌博奖池…")
    now = current_time()
    result = await _service(context).open(
        OpenGamble(
            user.id,
            chat.id,
            placeholder.message_id,
            now,
            now + _GAMBLE_DURATION,
            idempotency_key(update, "gamble:open", user.id),
        )
    )
    if result.code is GambleCode.NOT_REGISTERED:
        await placeholder.edit_text("请先使用 /me 命令注册后再开始游戏。")
    elif result.code is GambleCode.PERMISSION_DENIED:
        await placeholder.edit_text("您的权限不足，无法使用赌博命令。")
    elif result.code is GambleCode.ALREADY_ACTIVE:
        await placeholder.edit_text("赌博已在进行中，请等待本局结束。")
    elif result.session is not None:
        if result.replayed:
            await context.bot.edit_message_text(
                chat_id=result.session.chat_id,
                message_id=result.session.message_id,
                text=_gamble_text(result.session),
                reply_markup=_gamble_keyboard(result.session),
            )
            await placeholder.edit_text("已恢复上一条赌博开局消息。")
        else:
            await placeholder.edit_text(
                _gamble_text(result.session),
                reply_markup=_gamble_keyboard(result.session),
            )


async def gamble_callback(update: Update, context: TelegramContext) -> None:
    """@brief 解析会话绑定奖池 callback / Parse a session-bound pool callback.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    service = _service(context)
    try:
        if query.data.startswith("gamble_"):
            amount = int(query.data.removeprefix("gamble_"))
            active = await service.active(current_time())
            if active.session is None:
                await query.answer("当前没有活动的赌博游戏", show_alert=True)
                return
            session_id = active.session.session_id
        else:
            prefix, raw_session, raw_amount = query.data.split(":", 2)
            if prefix != "gamble":
                raise ValueError("Invalid gamble callback prefix")
            session_id = GameSessionId(UUID(hex=raw_session))
            amount = int(raw_amount)
    except ValueError, TypeError:
        await query.answer("按钮数据无效，请重新开始游戏。", show_alert=True)
        return
    user = query.from_user
    result = await service.place_bet(
        PlaceGambleBet(
            session_id,
            user.id,
            user.username or user.first_name,
            amount,
            None,
            current_time(),
            idempotency_key(update, "gamble:bet", user.id),
        )
    )
    if result.code is GambleCode.ALREADY_JOINED:
        await query.answer("您已参与，请等待开奖。", show_alert=True)
    elif result.code is GambleCode.INSUFFICIENT_COINS:
        await query.answer("您的硬币不足", show_alert=True)
    elif result.code in {GambleCode.NO_ACTIVE_SESSION, GambleCode.EXPIRED}:
        await query.answer("当前没有活动的赌博游戏", show_alert=True)
    elif result.code is not GambleCode.SUCCESS or result.session is None:
        await query.answer("处理押注失败，请稍后再试。", show_alert=True)
    else:
        if result.replayed:
            active = await service.active(current_time())
            if active.session is None or active.session.session_id != session_id:
                await query.answer(
                    f"已成功押注 {amount} 金币，当前游戏已进入结算。",
                    show_alert=True,
                )
                return
            session = active.session
        else:
            session = result.session
        await query.edit_message_text(
            _gamble_text(session),
            reply_markup=_gamble_keyboard(session),
        )
        await query.answer(f"成功押注 {amount} 金币，等待开奖", show_alert=True)


class TelegramGambleSettlementRenderer(GambleSettlementRenderer):
    """@brief Telegram 奖池结算文本渲染器 / Telegram pool-settlement text renderer."""

    def render(self, settlement: GambleSettlement) -> str:
        """@brief 渲染最终开奖文本 / Render final settlement text.

        @param settlement 已提交结算 / Committed settlement.
        @return Telegram 纯文本 / Telegram plain text.
        """

        if settlement.winner_id is None:
            return "本局赌博无人参与！"
        participants = _render_gamble_bets(settlement.session, max_chars=3500)
        return (
            "开奖时间到！\n"
            f"中奖者：@{settlement.winner_name}\n"
            f"获得奖池所有 {settlement.prize} 金币！\n\n"
            f"参与详情：\n{participants}"
        )
