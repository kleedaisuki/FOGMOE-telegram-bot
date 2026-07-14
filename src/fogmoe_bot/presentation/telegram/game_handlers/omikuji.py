"""@brief 御神签 Telegram 适配器 / Telegram adapter for Omikuji."""

from __future__ import annotations

from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from fogmoe_bot.application.games.omikuji.models import OmikujiCode
from fogmoe_bot.application.games.omikuji.service import (
    OMIKUJI_SERVICE_DATA_KEY,
    OmikujiService,
)
from fogmoe_bot.domain.games import FortuneLevel

from .common import TelegramContext, current_time, idempotency_key

_FORTUNE_COPY: Final[dict[FortuneLevel, tuple[str, str, str, str, str]]] = {
    FortuneLevel.GREAT_BLESSING: (
        "这是最高等级的好运，今天的一切都会顺利进行。",
        "身体健康充满活力，远离疾病。",
        "爱情方面可能会有意外的惊喜，单身者可能遇到心仪的对象。",
        "事业上会有重大突破，努力将得到回报。",
        "充分利用今天的好运，大胆追求自己的目标。",
    ),
    FortuneLevel.MIDDLE_BLESSING: (
        "运势很好，虽然不是最顶级，但也足够让你度过美好的一天。",
        "身体状况良好，保持适当运动可以更加健康。",
        "感情稳定发展，与伴侣沟通顺畅。",
        "工作顺利，可能会得到上司的赏识。",
        "保持积极心态，继续坚持当前的努力方向。",
    ),
    FortuneLevel.SMALL_BLESSING: (
        "运势偏好，可能会有一些小小的好事发生。",
        "身体无大碍，但需要注意休息。",
        "感情生活平稳，需要更多关心对方。",
        "工作中可能有小成就，但也要防止骄傲。",
        "脚踏实地，不要急于求成。",
    ),
    FortuneLevel.LATE_BLESSING: (
        "运势一般，不好不坏，需要谨慎行事。",
        "注意身体，避免过度疲劳。",
        "感情上可能有些小波折，需要耐心沟通。",
        "工作中会遇到一些挑战，保持冷静应对。",
        "凡事三思而后行，不要冲动决策。",
    ),
    FortuneLevel.CURSE: (
        "运势不佳，可能会遇到一些麻烦。",
        "身体可能感到不适，应该多注意休息。",
        "感情可能会有矛盾，需要多一些包容和理解。",
        "工作中可能会遇到困难，需要谨慎处理。",
        "放松心态，遇事不要太过着急，等待好时机。",
    ),
    FortuneLevel.GREAT_CURSE: (
        "运势很差，可能会遇到较大的困难。",
        "身体可能会感到不适，应该注意休息并避免剧烈运动。",
        "感情可能会遇到严重的挫折，需要冷静思考。",
        "工作中可能会遇到重大障碍，需要寻求他人帮助。",
        "今天应尽量避免重大决策，保持低调，等待运势好转。",
    ),
}
"""@brief 御神签各栏旧首选文案 / Legacy first-choice copy for each Omikuji section."""


def _service(context: TelegramContext) -> OmikujiService:
    """@brief 读取御神签 capability / Read the Omikuji capability."""

    value = context.application.bot_data.get(OMIKUJI_SERVICE_DATA_KEY)
    if not isinstance(value, OmikujiService):
        raise RuntimeError("Omikuji service was not assembled")
    return value


async def omikuji_command(update: Update, context: TelegramContext) -> None:
    """@brief 原子执行每日御神签 / Atomically execute the daily Omikuji draw.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    now = current_time()
    result = await _service(context).draw(
        user_id=user.id,
        day=now.date(),
        idempotency_key=idempotency_key(update, "omikuji:draw", user.id),
    )
    if result.code is OmikujiCode.NOT_REGISTERED:
        await message.reply_text(
            "您需要先注册个人信息才能使用御神签功能。\n请使用 /me 命令完成注册后再来抽签吧！"
        )
        return
    if result.code is OmikujiCode.INSUFFICIENT_FREE_TOKENS:
        await message.reply_text(
            "您的免费金币（Free）不足，无法进行祈愿抽签。每次抽签需要 1 枚 Free 金币作为供奉。\n"
            "可使用 /request_tokens <数量> <用途> 申请免费金币。"
        )
        return
    if result.fortune is None:
        await message.reply_text("抱歉，抽签过程中出现错误。请稍后再试。")
        return
    text = _render_fortune(user.username or user.first_name, result.fortune)
    if result.code is OmikujiCode.ALREADY_DRAWN:
        text += "\n\n您今天已经抽过御神签了。每人每天只能抽取一次，明天再来吧！"
        await message.reply_text(text)
        return
    label = "✨ 接受好运 ✨" if result.fortune.is_favorable else "🙏 祈求平安 🙏"
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=(f"omikuji:{user.id}:{result.fortune.value}"),
                    )
                ]
            ]
        ),
    )


def _render_fortune(display_name: str, fortune: FortuneLevel) -> str:
    """@brief 渲染御神签 / Render an Omikuji fortune.

    @param display_name 展示名 / Display name.
    @param fortune 运势 / Fortune.
    @return 纯文本 / Plain text.
    """

    description, health, love, career, advice = _FORTUNE_COPY[fortune]
    return (
        f"🔮 {display_name}的今日运势 🔮\n\n"
        f"结果: {fortune.value}\n\n{description}\n\n"
        f"健康: {health}\n爱情: {love}\n事业/学业: {career}\n\n建议: {advice}"
    )


async def omikuji_callback(update: Update, context: TelegramContext) -> None:
    """@brief 处理御神签确认按钮 / Handle the Omikuji acknowledgement button.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    del context
    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    try:
        if query.data.startswith("omikuji_"):
            prefix, raw_fortune, raw_user_id = query.data.split("_", 2)
        else:
            prefix, raw_user_id, raw_fortune = query.data.split(":", 2)
        if prefix != "omikuji":
            raise ValueError("Invalid Omikuji prefix")
        user_id = int(raw_user_id)
        fortune = FortuneLevel(raw_fortune)
    except ValueError:
        await query.answer("按钮数据无效，请尝试重新抽签", show_alert=True)
        return
    if query.from_user.id != user_id:
        await query.answer("这不是您的御神签，无法进行互动。", show_alert=True)
        return
    original = (
        query.message.text
        if isinstance(query.message, Message) and query.message.text
        else "御神签"
    )
    if fortune.is_favorable:
        await query.answer("好运已经接受，愿它伴随着您！", show_alert=True)
        suffix = f"✨ {query.from_user.first_name} 已接受好运 ✨"
    else:
        await query.answer("您已将不好的运势留在了神社，祈求平安！", show_alert=True)
        suffix = f"🙏 {query.from_user.first_name} 已祈求平安 🙏"
    await query.edit_message_text(f"{original}\n\n{suffix}")
