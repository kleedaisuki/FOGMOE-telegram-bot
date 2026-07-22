"""将 Telegram 音乐命令映射到 typed use cases / Map Telegram music commands to typed use cases."""

from __future__ import annotations

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.media.music_service import (
    MUSIC_SERVICE_DATA_KEY,
    MusicHelp,
    MusicNotRegistered,
    MusicPage,
    MusicRateLimited,
    MusicService,
    MusicSessionExpired,
    MusicUnavailable,
)
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import MusicPlatform, MusicSearchId

_MUSIC_PAGE_CALLBACK = re.compile(r"^music_p_([0-9a-f]{32})_([0-9]{1,3})$")
_MUSIC_SWITCH_CALLBACK = re.compile(
    r"^music_s_([0-9a-f]{32})_(wy|qq|kw|mg|qi)_([0-9]{1,3})$"
)

MUSIC_HELP = """
🎵 **音乐搜索使用说明** 🎵

基本命令:
• `/music <关键词>` - 搜索歌曲信息
• `/music help` - 显示此帮助信息

高级选项:
• 搜索后可以选择不同的音乐平台
• 支持网易云音乐、QQ音乐、酷我音乐、咪咕音乐、千千音乐
• 支持翻页查看更多结果

提示：
• 搜索结果显示歌曲名称、专辑、歌手等信息
• 如需精确搜索，可使用完整歌名，如：`/music again`
"""


type TelegramContext = ContextTypes.DEFAULT_TYPE


def _service(context: TelegramContext) -> MusicService:
    value = context.application.bot_data.get(MUSIC_SERVICE_DATA_KEY)
    if not isinstance(value, MusicService):
        raise RuntimeError("music service capability is not configured")
    return value


async def music_command(update: Update, context: TelegramContext) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    query_text = " ".join(context.args or ())
    result = await _service(context).search(
        user_id=UserId(user.id),
        query=query_text,
    )
    mention = f"@{user.username or user.id}"
    if isinstance(result, MusicHelp):
        await message.reply_text(MUSIC_HELP, parse_mode="Markdown")
        return
    if isinstance(result, MusicNotRegistered):
        await message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用此功能。\n"
            "Please register first using the /me command before using this feature."
        )
        return
    if isinstance(result, MusicRateLimited):
        await message.reply_text(
            f"{mention} 您的搜索频率过快，请 {result.retry_after_seconds} 秒后再试。"
        )
        return
    if isinstance(result, MusicUnavailable):
        await message.reply_text(
            f"{mention} 未找到与 “{html.escape(query_text)}” 相关的歌曲信息。"
        )
        return
    if not isinstance(result, MusicPage):
        raise AssertionError("unhandled music result")
    await _send_music_page(message, result, mention=mention)


async def music_callback(update: Update, context: TelegramContext) -> None:
    user = update.effective_user
    query = update.callback_query
    if user is None or query is None or not isinstance(query.data, str):
        return
    data = query.data
    if data == "music_info":
        await query.answer("当前页码/总页数")
        return
    page_match = _MUSIC_PAGE_CALLBACK.fullmatch(data)
    switch_match = _MUSIC_SWITCH_CALLBACK.fullmatch(data)
    if page_match is None and switch_match is None:
        await query.answer("无效的回调数据", show_alert=True)
        return
    service = _service(context)
    if page_match is not None:
        result = await service.page(
            user_id=UserId(user.id),
            search_id=MusicSearchId(page_match.group(1)),
            page=int(page_match.group(2)),
        )
    else:
        assert switch_match is not None
        result = await service.switch_platform(
            user_id=UserId(user.id),
            search_id=MusicSearchId(switch_match.group(1)),
            platform=MusicPlatform(switch_match.group(2)),
            page=int(switch_match.group(3)),
        )
    if isinstance(result, MusicRateLimited):
        await query.answer(
            f"您的点击频率过快，请 {result.retry_after_seconds} 秒后再试。",
            show_alert=True,
        )
        return
    if isinstance(result, MusicSessionExpired):
        await query.answer("搜索结果已过期，请重新使用 /music", show_alert=True)
        return
    if isinstance(result, MusicUnavailable):
        await query.answer("未找到歌曲信息，请稍后再试", show_alert=True)
        return
    if not isinstance(result, MusicPage):
        raise AssertionError("unhandled music callback result")
    await query.answer()
    if isinstance(query.message, Message):
        await _send_music_page(query.message, result, edit=True)


async def _send_music_page(
    message: Message,
    page: MusicPage,
    *,
    mention: str | None = None,
    edit: bool = False,
) -> None:
    """渲染并发送或编辑音乐页 / Render and send or edit a music page."""

    session = page.session
    prefix = f"{html.escape(mention)} " if mention else ""
    lines = [
        f"{prefix}搜索结果 - “{html.escape(session.query)}” ({session.platform.display_name})：",
        "",
    ]
    start = (page.page - 1) * 5 + 1
    for index, track in enumerate(page.tracks, start=start):
        lines.extend(
            (
                f"{index}. {html.escape(track.name)}",
                f"   👤 歌手：{html.escape(track.artist)}",
                f"   💿 专辑：{html.escape(track.album)}",
                f"   🎵 平台：{track.platform.display_name}",
                f'   🆔 ID：<a href="{html.escape(track.platform.track_url(track.track_id), quote=True)}">'
                f"{html.escape(track.track_id)}</a>",
                "",
            )
        )
    if page.total_pages > 1:
        lines.append(f"第 {page.page}/{page.total_pages} 页")
    keyboard = _music_keyboard(page)
    text = "\n".join(lines)
    if edit:
        await message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


def _music_keyboard(page: MusicPage) -> InlineKeyboardMarkup:
    """构造有界短 callback_data 键盘 / Build a bounded keyboard with short callback data."""

    search_id = page.session.search_id
    rows: list[list[InlineKeyboardButton]] = []
    if page.total_pages > 1:
        controls: list[InlineKeyboardButton] = []
        if page.page > 1:
            controls.append(
                InlineKeyboardButton(
                    "◀️ 上一页",
                    callback_data=f"music_p_{search_id}_{page.page - 1}",
                )
            )
        controls.append(
            InlineKeyboardButton(
                f"{page.page}/{page.total_pages}",
                callback_data="music_info",
            )
        )
        if page.page < page.total_pages:
            controls.append(
                InlineKeyboardButton(
                    "下一页 ▶️",
                    callback_data=f"music_p_{search_id}_{page.page + 1}",
                )
            )
        rows.append(controls)
    platform_buttons = [
        InlineKeyboardButton(
            platform.display_name,
            callback_data=f"music_s_{search_id}_{platform.value}_1",
        )
        for platform in MusicPlatform
        if platform is not page.session.platform
    ]
    rows.extend(
        platform_buttons[index : index + 3]
        for index in range(0, len(platform_buttons), 3)
    )
    return InlineKeyboardMarkup(rows)
