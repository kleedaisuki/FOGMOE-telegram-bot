"""将 Telegram 图片命令映射到 typed use cases / Map Telegram picture commands to typed use cases."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.media.picture_ports import PictureDeliveryTarget
from fogmoe_bot.application.media.picture_service import (
    HdDeliveryReady,
    HdUnavailable,
    PICTURE_SERVICE_DATA_KEY,
    PictureHelp,
    PictureInsufficientCoins,
    PictureNotRegistered,
    PicturePermissionDenied,
    PictureReady,
    PictureService,
    PictureUnavailable,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_PHOTO,
    OutboundDraft,
)
from fogmoe_bot.domain.media.identifiers import ArtifactId, UserId
from fogmoe_bot.domain.media.picture import HdOffer, PictureCandidate, PictureRating

from ..delivery import delivery_stream_for_chat
from ..idempotency import telegram_update_idempotency_key
from ..update_mapper import TelegramUpdateMapper


_HD_CALLBACK = re.compile(r"^pic_hd_([0-9a-f]{32})$")

logger = logging.getLogger(__name__)

PICTURE_HELP = """
📷 **图片命令使用说明** 📷

基本命令:
• `/pic` - 随机获取一张图片，消耗5金币
• `/pic help` - 显示此帮助信息

高级选项:
• `/pic nsfw` - 获取成人内容图片，消耗5金币 (需要权限等级≥2)
• 点击高清图片按钮 - 获取原图，额外消耗10金币

注意事项:
• 所有图片均从公开图库随机获取
• 使用成人内容选项需要足够的权限
• 部分图片可能无法显示，金币将自动退还
"""


type TelegramContext = ContextTypes.DEFAULT_TYPE


class TelegramPicturePreviewOutboundFactory:
    """将图片报价映射为严格 Telegram photo 意图 / Map picture offers to strict Telegram photo intents."""

    def create(
        self,
        *,
        target: PictureDeliveryTarget,
        offer: HdOffer,
        preview_cost: int,
        hd_cost: int,
        idempotency_key: str,
        created_at: datetime,
    ) -> OutboundDraft:
        """构造确定性 standalone outbox 草稿 / Build a deterministic standalone outbox draft."""

        outbound_key = f"{idempotency_key}:photo"
        picture = offer.picture
        has_hd = bool(picture.file_url and picture.file_url != picture.preview_url)
        payload: JsonObject = {
            "chat_id": target.chat_id,
            "photo_url": picture.preview_url,
            "caption": _picture_caption(
                target.mention,
                picture,
                preview_cost,
                has_hd,
            ),
            "has_spoiler": picture.rating is PictureRating.NSFW,
            "message_thread_id": target.message_thread_id,
            "reply_to_message_id": target.reply_to_message_id,
        }
        if has_hd:
            payload.update(
                {
                    "button_text": f"查看高清原图 ({hd_cost}金币)",
                    "button_callback_data": f"pic_hd_{offer.offer_id}",
                }
            )
        return OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                target.conversation_id,
                outbound_key,
            ),
            conversation_id=target.conversation_id,
            turn_id=None,
            delivery_stream_id=target.delivery_stream_id,
            kind=SEND_TELEGRAM_PHOTO,
            payload=payload,
            idempotency_key=outbound_key,
            created_at=created_at,
        )


def _service(context: TelegramContext) -> PictureService:
    value = context.application.bot_data.get(PICTURE_SERVICE_DATA_KEY)
    if not isinstance(value, PictureService):
        raise RuntimeError("picture service capability is not configured")
    return value


async def pic_command(update: Update, context: TelegramContext) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    args = tuple(context.args or ())
    first_arg = args[0].casefold() if args else ""
    rating = PictureRating.NSFW if first_arg == "nsfw" else PictureRating.SAFE
    mention = f"@{user.username or user.id}"
    message_thread_id = getattr(message, "message_thread_id", None)
    target = PictureDeliveryTarget(
        conversation_id=TelegramUpdateMapper().identity_for(update).conversation_id,
        delivery_stream_id=delivery_stream_for_chat(
            message.chat_id,
            message_thread_id,
        ),
        chat_id=message.chat_id,
        message_thread_id=message_thread_id,
        reply_to_message_id=message.message_id,
        mention=mention,
    )
    result = await _service(context).request_picture(
        user_id=UserId(user.id),
        rating=rating,
        idempotency_key=telegram_update_idempotency_key(update, "media.pic"),
        target=target,
        explicit_help=first_arg == "help",
    )
    if isinstance(result, PictureHelp):
        prefix = (
            f"{mention} 这是您24小时内首次使用图片命令，以下是帮助信息：\n\n"
            if result.first_use
            else ""
        )
        await message.reply_text(prefix + PICTURE_HELP, parse_mode="Markdown")
        return
    if isinstance(result, PictureNotRegistered):
        await message.reply_text(
            "请先使用 /me 命令注册个人信息后再使用此功能。\n"
            "Please register first using the /me command before using this feature."
        )
        return
    if isinstance(result, PicturePermissionDenied):
        await message.reply_text(
            f"您的权限等级不足，需要权限等级≥{result.required}才能使用NSFW选项。\n"
            "Your permission level is not enough. You can purchase permission levels in /shop."
        )
        return
    if isinstance(result, PictureInsufficientCoins):
        await message.reply_text(
            f"{mention} 您的金币不足！使用此功能需要 {result.required} 个金币，"
            f"您当前有 {result.balance} 个金币。\nNot enough coins!"
        )
        return
    if isinstance(result, PictureUnavailable):
        await message.reply_text(
            f"{mention} 获取图片失败，请稍后再试。未扣除金币。\n"
            "Failed to fetch image. No coins were charged."
        )
        return
    if not isinstance(result, PictureReady):
        raise AssertionError("unhandled picture result")
    # 图片、扣费与严格 photo intent 已由同一事务提交，outbox worker 是唯一发送者。


async def hd_pic_callback(update: Update, context: TelegramContext) -> None:
    user = update.effective_user
    chat = update.effective_chat
    query = update.callback_query
    if user is None or chat is None or query is None or not isinstance(query.data, str):
        return
    matched = _HD_CALLBACK.fullmatch(query.data)
    if matched is None:
        await query.answer("无效的请求数据", show_alert=True)
        return
    offer_id = ArtifactId(matched.group(1))
    service = _service(context)
    result = await service.request_hd(
        offer_id=offer_id,
        user_id=UserId(user.id),
    )
    if isinstance(result, HdUnavailable):
        text = {
            "missing": "图片数据已过期，请重新获取",
            "expired": "图片数据已过期，请重新获取",
            "busy": "此图片正在处理中，请勿重复点击",
            "delivered": "此高清图片已经获取",
            "insufficient": (
                f"金币不足！查看高清图片需要 {service.policy.hd_cost} 个金币，"
                f"您当前有 {result.balance or 0} 个金币。"
            ),
        }.get(result.code, "高清图片暂不可用")
        await query.answer(text, show_alert=True)
        return
    if not isinstance(result, HdDeliveryReady):
        raise AssertionError("unhandled HD result")
    await query.answer("正在处理您的高清图片请求...")
    charged_user = result.offer.charged_user_id or UserId(user.id)
    mention = (
        f"@{user.username or charged_user}"
        if user.id == int(charged_user)
        else str(charged_user)
    )
    caption = f"{mention} 消耗了 {service.policy.hd_cost} 金币获取此高清图片" + (
        " (NSFW内容)" if result.offer.picture.rating is PictureRating.NSFW else ""
    )
    original_message_id = getattr(query.message, "message_id", None)
    try:
        if result.content is not None:
            document = BytesIO(result.content)
            document.name = result.filename
            await context.bot.send_document(
                chat_id=chat.id,
                document=document,
                filename=result.filename,
                caption=caption,
                reply_to_message_id=original_message_id,
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"{mention} 可通过下方链接下载高清原图。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("下载高清原图", url=result.fallback_url)]]
                ),
                reply_to_message_id=original_message_id,
            )
    except Exception:
        logger.exception("HD delivery permanently failed offer_id=%s", offer_id)
        try:
            await service.refund_hd(offer_id)
        except Exception:
            logger.exception("HD refund failed offer_id=%s", offer_id)
        await query.answer(
            "发送高清图片时出错；已尝试退款，若未到账请稍后重试原按钮。",
            show_alert=True,
        )
        return
    try:
        await service.complete_hd(offer_id)
    except Exception:
        logger.exception(
            "HD was delivered but confirmation failed; preserving the claim for lease recovery "
            "offer_id=%s",
            offer_id,
        )
        return
    try:
        await query.edit_message_caption(caption=caption, reply_markup=None)
    except Exception:
        logger.warning("Could not remove HD callback button offer_id=%s", offer_id)


def _picture_caption(
    mention: str,
    picture: PictureCandidate,
    cost: int,
    has_hd: bool,
) -> str:
    """格式化图片 caption / Format a picture caption."""

    lines = [f"{mention} 消耗了 {cost} 金币获取此图片。"]
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
    if has_hd:
        lines.append("\n点击下方按钮可获取高清原图，需额外消耗金币。")
    caption = "\n".join(lines)
    return caption if len(caption) <= 1024 else caption[:1021] + "..."
