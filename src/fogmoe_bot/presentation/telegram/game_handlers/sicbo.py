"""@brief 骰宝 Telegram 适配器 / Telegram adapter for Sic Bo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, cast
from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from fogmoe_bot.application.games.sicbo.models import (
    CancelSicBo,
    OpenSicBo,
    SelectSicBoBet,
    SicBoCode,
)
from fogmoe_bot.application.games.sicbo.service import (
    SICBO_SERVICE_DATA_KEY,
    SicBoService,
)
from fogmoe_bot.domain.games import (
    GameSessionId,
    SICBO_BET_NAMES,
    SICBO_PAYOUTS,
    SicBoBet,
    SicBoOutcome,
    SicBoSession,
)

from .common import TelegramContext, current_time, idempotency_key

_SICBO_SESSION_DURATION = timedelta(minutes=10)
"""@brief 骰宝选择流程过期时间 / Sic Bo selection-flow expiration."""

_DICE_EMOJI: Final = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
"""@brief 骰面到 Unicode 字符 / Die-face to Unicode glyph."""


def _service(context: TelegramContext) -> SicBoService:
    """@brief 读取骰宝 capability / Read the Sic Bo capability."""

    value = context.application.bot_data.get(SICBO_SERVICE_DATA_KEY)
    if not isinstance(value, SicBoService):
        raise RuntimeError("Sic Bo service was not assembled")
    return value


@dataclass(frozen=True, slots=True)
class _SicBoCallback:
    """@brief 骰宝 callback 值对象 / Sic Bo callback value object.

    @param session_id 会话 ID / Session ID.
    @param version 会话版本 / Session version.
    @param action UI 动作 / UI action.
    """

    session_id: GameSessionId
    version: int
    action: str

    def encode(self) -> str:
        """@brief 编码 callback / Encode the callback.

        @return Bot API 安全文本 / Bot-API-safe text.
        """

        value = f"sb:{self.session_id.value.hex}:{self.version}:{self.action}"
        if len(value.encode()) > 64:
            raise ValueError("Sic Bo callback exceeds Bot API limit")
        return value

    @classmethod
    def decode(cls, value: str) -> _SicBoCallback:
        """@brief 严格解析 callback / Strictly decode a callback.

        @param value callback 文本 / Callback text.
        @return 类型化 callback / Typed callback.
        """

        prefix, raw_session, raw_version, action = value.split(":", 3)
        if prefix != "sb" or not action:
            raise ValueError("Invalid Sic Bo callback")
        return cls(GameSessionId(UUID(hex=raw_session)), int(raw_version), action)


def _sicbo_button(
    label: str, session: SicBoSession, action: str
) -> InlineKeyboardButton:
    """@brief 构造一个版本绑定骰宝按钮 / Build one version-bound Sic Bo button.

    @param label 按钮文字 / Button label.
    @param session 会话 / Session.
    @param action 动作 / Action.
    @return Telegram 按钮 / Telegram button.
    """

    return InlineKeyboardButton(
        label,
        callback_data=_SicBoCallback(
            session.session_id, session.version, action
        ).encode(),
    )


def _sicbo_main_keyboard(session: SicBoSession) -> InlineKeyboardMarkup:
    """@brief 构造骰宝主菜单 / Build the Sic Bo main menu.

    @param session 会话 / Session.
    @return Telegram keyboard / Telegram keyboard.
    """

    return InlineKeyboardMarkup(
        [
            [
                _sicbo_button("大 (11-17)", session, "b:big"),
                _sicbo_button("小 (4-10)", session, "b:small"),
            ],
            [
                _sicbo_button("单 (奇数)", session, "b:odd"),
                _sicbo_button("双 (偶数)", session, "b:even"),
            ],
            [
                _sicbo_button("总和 (4-10)", session, "menu:sumlow"),
                _sicbo_button("总和 (11-17)", session, "menu:sumhigh"),
            ],
            [
                _sicbo_button("任意围骰", session, "b:any_triple"),
                _sicbo_button("特定围骰", session, "menu:triples"),
            ],
            [_sicbo_button("❌ 取消", session, "cancel")],
        ]
    )


def _sicbo_selection_keyboard(
    session: SicBoSession, bets: tuple[SicBoBet, ...]
) -> InlineKeyboardMarkup:
    """@brief 构造总和或围骰子菜单 / Build a sum/triple submenu.

    @param session 会话 / Session.
    @param bets 可选下注 / Available bets.
    @return Telegram keyboard / Telegram keyboard.
    """

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(bets), 2):
        rows.append(
            [
                _sicbo_button(
                    f"{SICBO_BET_NAMES[bet]} (赔率{SICBO_PAYOUTS[bet]}:1)",
                    session,
                    f"b:{bet.value}",
                )
                for bet in bets[index : index + 2]
            ]
        )
    rows.append(
        [
            _sicbo_button("⬅️ 返回", session, "menu:main"),
            _sicbo_button("❌ 取消", session, "cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _sicbo_amount_keyboard(session: SicBoSession) -> InlineKeyboardMarkup:
    """@brief 构造骰宝金额键盘 / Build the Sic Bo amount keyboard.

    @param session 会话 / Session.
    @return Telegram keyboard / Telegram keyboard.
    """

    return InlineKeyboardMarkup(
        [
            [
                _sicbo_button(f"{amount} 金币", session, f"a:{amount}")
                for amount in (1, 5, 10)
            ],
            [
                _sicbo_button(f"{amount} 金币", session, f"a:{amount}")
                for amount in (20, 50, 100)
            ],
            [_sicbo_button("❌ 取消", session, "cancel")],
        ]
    )


async def sicbo_command(update: Update, context: TelegramContext) -> None:
    """@brief 解析 /sicbo 并开启 durable 会话 / Parse /sicbo and open a durable session.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if user is None or message is None or chat is None:
        return
    placeholder = await message.reply_text("正在开启骰宝…")
    now = current_time()
    result = await _service(context).open(
        OpenSicBo(
            user.id,
            chat.id,
            placeholder.message_id,
            now,
            now + _SICBO_SESSION_DURATION,
            idempotency_key(update, "sicbo:open", user.id),
        )
    )
    if result.code is SicBoCode.NOT_REGISTERED:
        await placeholder.edit_text("请先使用 /me 命令注册后再开始游戏。")
    elif result.code is SicBoCode.INSUFFICIENT_COINS:
        await placeholder.edit_text("您的金币不足，至少需要1枚金币才能开始游戏。")
    elif result.code is SicBoCode.ALREADY_ACTIVE:
        await placeholder.edit_text("您已经在一个骰宝游戏中，请先完成当前游戏。")
    elif result.session is not None:
        if result.replayed:
            await context.bot.edit_message_text(
                chat_id=result.session.chat_id,
                message_id=result.session.message_id,
                text="🎲 骰宝游戏 🎲\n\n请选择您的下注类型：",
                reply_markup=_sicbo_main_keyboard(result.session),
            )
            await placeholder.edit_text("已恢复上一条骰宝开局消息。")
        else:
            await placeholder.edit_text(
                "🎲 骰宝游戏 🎲\n\n请选择您的下注类型：",
                reply_markup=_sicbo_main_keyboard(result.session),
            )


async def sicbo_callback(update: Update, context: TelegramContext) -> None:
    """@brief 驱动 durable 骰宝状态机 / Drive the durable Sic Bo state machine.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    user_id = query.from_user.id
    service = _service(context)
    try:
        if query.data.startswith("sicbo_"):
            parts = query.data.split("_")
            legacy_user_id = int(parts[1])
            if legacy_user_id != user_id:
                await query.answer(
                    "这不是您的游戏，请使用 /sicbo 开始自己的游戏", show_alert=True
                )
                return
            active = await service.active(user_id, current_time())
            if active is None:
                await query.answer("游戏已结束或已过期。", show_alert=True)
                return
            legacy_action = "_".join(parts[2:])
            action = {
                "back_to_main": "menu:main",
                "sum_low": "menu:sumlow",
                "sum_high": "menu:sumhigh",
                "specific_triples": "menu:triples",
                "cancel": "cancel",
            }.get(legacy_action)
            if action is None and legacy_action.startswith("bet_"):
                action = f"b:{legacy_action.removeprefix('bet_')}"
            if action is None and legacy_action.startswith("amount_"):
                action = f"a:{legacy_action.removeprefix('amount_')}"
            if action is None:
                raise ValueError("Unknown legacy Sic Bo action")
            callback = _SicBoCallback(active.session_id, active.version, action)
        else:
            callback = _SicBoCallback.decode(query.data)
    except ValueError, TypeError, IndexError:
        await query.answer("按钮数据无效，请使用 /sicbo 重新开始。", show_alert=True)
        return
    if callback.action.startswith("menu:"):
        session = await service.active(user_id, current_time())
        if session is None or session.session_id != callback.session_id:
            await query.answer("游戏已结束或已过期。", show_alert=True)
            return
        menu = callback.action.removeprefix("menu:")
        if menu == "main":
            await query.edit_message_text(
                "🎲 骰宝游戏 🎲\n\n请选择您的下注类型：",
                reply_markup=_sicbo_main_keyboard(session),
            )
        elif menu == "sumlow":
            bets = tuple(SicBoBet(f"sum_{value}") for value in range(4, 11))
            await query.edit_message_text(
                "请选择要下注的总和点数 (4-10)：",
                reply_markup=_sicbo_selection_keyboard(session, bets),
            )
        elif menu == "sumhigh":
            bets = tuple(SicBoBet(f"sum_{value}") for value in range(11, 18))
            await query.edit_message_text(
                "请选择要下注的总和点数 (11-17)：",
                reply_markup=_sicbo_selection_keyboard(session, bets),
            )
        elif menu == "triples":
            bets = tuple(SicBoBet(f"triple_{value}") for value in range(1, 7))
            await query.edit_message_text(
                "请选择要下注的特定围骰：",
                reply_markup=_sicbo_selection_keyboard(session, bets),
            )
        await query.answer()
        return
    if callback.action == "cancel":
        result = await service.cancel(
            CancelSicBo(
                callback.session_id,
                user_id,
                callback.version,
                current_time(),
                idempotency_key(update, "sicbo:cancel", user_id),
            )
        )
        if result.code is SicBoCode.SUCCESS:
            await query.edit_message_text("骰宝游戏已取消。")
            await query.answer("游戏已取消")
        else:
            await _answer_sicbo_failure(query, result.code)
        return
    if callback.action.startswith("b:"):
        try:
            bet = SicBoBet(callback.action.removeprefix("b:"))
        except ValueError:
            await query.answer("下注类型无效。", show_alert=True)
            return
        result = await service.select_bet(
            SelectSicBoBet(
                callback.session_id,
                user_id,
                bet,
                callback.version,
                current_time(),
                idempotency_key(update, "sicbo:select", user_id),
            )
        )
        if result.code is SicBoCode.SUCCESS and result.session is not None:
            await query.edit_message_text(
                f"您选择了: {SICBO_BET_NAMES[bet]} "
                f"(赔率 {SICBO_PAYOUTS[bet]}:1)\n\n请选择您要下注的金币数量：",
                reply_markup=_sicbo_amount_keyboard(result.session),
            )
            await query.answer()
        else:
            await _answer_sicbo_failure(query, result.code)
        return
    if callback.action.startswith("a:"):
        try:
            amount = int(callback.action.removeprefix("a:"))
        except ValueError:
            await query.answer("下注金额无效。", show_alert=True)
            return
        result = await service.roll_and_play(
            session_id=callback.session_id,
            user_id=user_id,
            amount=amount,
            expected_version=callback.version,
            now=current_time(),
            idempotency_key=idempotency_key(update, "sicbo:play", user_id),
        )
        if result.code is SicBoCode.SUCCESS and result.outcome is not None:
            await query.edit_message_text(
                _render_sicbo_outcome(result.outcome, result.balance)
            )
            await query.answer()
        else:
            await _answer_sicbo_failure(query, result.code)
        return
    await query.answer("未知骰宝操作。", show_alert=True)


async def _answer_sicbo_failure(query: object, code: SicBoCode) -> None:
    """@brief 将骰宝业务失败映射为 callback alert / Map a Sic Bo failure to a callback alert.

    @param query PTB CallbackQuery / PTB CallbackQuery.
    @param code 业务代码 / Business code.
    @return None / None.
    """

    from telegram import CallbackQuery

    callback_query = cast(CallbackQuery, query)
    text = {
        SicBoCode.STALE_VERSION: "按钮已过期，请使用 /sicbo 重新开始。",
        SicBoCode.EXPIRED: "游戏已过期，请使用 /sicbo 重新开始。",
        SicBoCode.INSUFFICIENT_COINS: "您的金币不足。",
        SicBoCode.NO_ACTIVE_SESSION: "游戏已结束或已取消。",
    }.get(code, "处理游戏时出现错误，请稍后再试。")
    await callback_query.answer(text, show_alert=True)


def _render_sicbo_outcome(outcome: SicBoOutcome, balance: int | None) -> str:
    """@brief 渲染骰宝终局 / Render a Sic Bo terminal result.

    @param outcome 结算结果 / Settlement result.
    @param balance 结算后余额 / Post-settlement balance.
    @return 纯文本 / Plain text.
    """

    dice = " ".join(_DICE_EMOJI[face] for face in outcome.roll.dice)
    result = "、".join(outcome.roll.features())
    text = (
        "🎲 骰宝游戏结果 🎲\n\n"
        f"骰子点数: {dice} = {outcome.roll.total}\n"
        f"结果特性: {result}\n\n"
        f"您下注: {SICBO_BET_NAMES[outcome.bet]} {outcome.amount} 金币\n"
        f"{'恭喜您赢了! 🎉' if outcome.won else '很遗憾，您输了! 😔'}\n"
    )
    if outcome.won:
        text += f"赔率: {SICBO_PAYOUTS[outcome.bet]}:1\n获得: {outcome.credited} 金币"
    else:
        text += f"您损失了 {outcome.amount} 金币"
    return (
        f"{text}\n\n当前余额: {balance or 0} 金币\n\n如需再玩一次，请使用 /sicbo 命令。"
    )
