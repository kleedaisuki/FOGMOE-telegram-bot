"""@brief Telegram 免费图片入口 / Telegram free-picture ingress.

公开 `/pic` 只有随机免费预览。付费预览、高清报价和它们的 callback 已被移除，因而此
入口没有任何可达的余额变更能力。
/ Public `/pic` serves random free previews only.  Paid previews, HD offers, and their callbacks
are removed, so this ingress has no reachable balance-mutation capability.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.media.picture_service import (
    PICTURE_SERVICE_DATA_KEY,
    PictureFreeReady,
    PictureNotRegistered,
    PicturePermissionDenied,
    PictureService,
    PictureUnavailable,
)
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating

PICTURE_FREE_HELP = (
    "📷 图片功能（免费预览）\n\n"
    "• `/pic` — 随机获取一张安全图片\n"
    "• `/pic nsfw` — 获取成人内容图片（需权限等级 ≥ 2）\n"
    "• `/pic help` — 显示此说明\n\n"
    "图片预览不消耗金币；系统不提供付费高清购买或领取。"
)
"""@brief 免费图片模式说明 / Free-picture mode help text."""


type TelegramContext = ContextTypes.DEFAULT_TYPE
"""@brief 默认 PTB 上下文类型 / Default PTB context type."""


def _service(context: TelegramContext) -> PictureService:
    """@brief 读取图片服务 capability / Read the picture-service capability.

    @param context PTB 上下文 / PTB context.
    @return 已装配的图片服务 / Assembled picture service.
    @raise RuntimeError capability 缺失或类型不匹配时抛出 / Raised when the capability is missing or has the wrong type.
    """

    value = context.application.bot_data.get(PICTURE_SERVICE_DATA_KEY)
    if not isinstance(value, PictureService):
        raise RuntimeError("picture service capability is not configured")
    return value


async def pic_command(update: Update, context: TelegramContext) -> None:
    """@brief 投递免费随机图片 / Deliver a free random picture.

    @param update Telegram 更新 / Telegram update.
    @param context PTB 上下文 / PTB context.
    @return None / None.
    @note 此入口仅调用 ``request_free_picture``；服务类型中也不存在收费 preview/HD 方法。
        / This ingress calls only ``request_free_picture``; the service type contains no charged
        preview or HD method.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    args = tuple(context.args or ())
    first_arg = args[0].casefold() if args else ""
    if first_arg == "help":
        await message.reply_text(PICTURE_FREE_HELP)
        return
    rating = PictureRating.NSFW if first_arg == "nsfw" else PictureRating.SAFE
    result = await _service(context).request_free_picture(
        user_id=UserId(user.id),
        rating=rating,
    )
    if isinstance(result, PictureNotRegistered):
        await message.reply_text("请先使用 /me 命令注册个人信息后再使用此功能。")
        return
    if isinstance(result, PicturePermissionDenied):
        await message.reply_text(
            f"您的权限等级不足，需要权限等级 ≥ {result.required} 才能使用 NSFW 选项。"
        )
        return
    if isinstance(result, PictureUnavailable):
        await message.reply_text("暂时无法获取图片，请稍后再试。")
        return
    if not isinstance(result, PictureFreeReady):
        raise AssertionError("unhandled free-picture result")
    await message.reply_photo(
        photo=result.picture.preview_url,
        caption=_free_picture_caption(f"@{user.username or user.id}", result.picture),
    )


def _free_picture_caption(mention: str, picture: PictureCandidate) -> str:
    """@brief 渲染无金币语义的免费预览 caption / Render a free-preview caption without token semantics.

    @param mention 请求者提及 / Requester mention.
    @param picture 已选择的图片 / Selected picture.
    @return 受 Telegram 长度限制的 caption / Caption bounded for Telegram.
    """

    lines = [f"{mention} 的随机图片（免费预览）"]
    if picture.rating is PictureRating.NSFW:
        lines.append("类型: NSFW")
    tags = picture.tags.split()[:10]
    if tags:
        lines.append("标签: " + " ".join(f"#{tag}" for tag in tags))
    stats: list[str] = []
    if picture.width is not None and picture.height is not None:
        stats.append(f"分辨率: {picture.width}x{picture.height}")
    if picture.file_size is not None:
        stats.append(f"文件大小: {picture.file_size / (1024 * 1024):.2f}MB")
    if picture.score is not None:
        stats.append(f"评分: {picture.score}")
    if stats:
        lines.append("统计信息: " + ", ".join(stats))
    caption = "\n".join(lines)
    return caption if len(caption) <= 1024 else caption[:1021] + "..."
