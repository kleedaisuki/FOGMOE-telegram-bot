"""@brief Telegram 免费图片入口测试 / Tests for the Telegram free-picture ingress."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from fogmoe_bot.application.media.picture_service import PictureFreeReady
from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating
from fogmoe_bot.presentation.telegram.media_handlers import picture as picture_handlers


def _picture() -> PictureCandidate:
    """@brief 构造免费预览图片 / Build a free-preview picture.

    @return 图片候选 / Picture candidate.
    """

    return PictureCandidate(
        source_id="handler-picture",
        sample_url="https://example.test/sample.jpg",
        file_url=None,
        tags="safe",
        width=100,
        height=100,
        file_size=1000,
        score=1,
        rating=PictureRating.SAFE,
    )


def _adapter(*, args: tuple[str, ...] = ()) -> tuple[Any, Any, Any]:
    """@brief 构造图片命令 adapter fakes / Build picture-command adapter fakes.

    @param args 命令参数 / Command arguments.
    @return update、context 与源消息 / Update, context, and source message.
    """

    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, username="klee"),
        effective_message=message,
    )
    context = SimpleNamespace(args=args)
    return update, context, message


def test_picture_command_only_exposes_the_free_preview_method(monkeypatch) -> None:
    """@brief `/pic` 只调用免费预览能力 / `/pic` calls only the free-preview capability.

    @param monkeypatch pytest monkeypatch / pytest monkeypatch.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行免费预览入口 / Exercise the free-preview ingress.

        @return None / None.
        """

        service = SimpleNamespace(
            request_free_picture=AsyncMock(return_value=PictureFreeReady(_picture()))
        )
        monkeypatch.setattr(picture_handlers, "_service", lambda context: service)
        update, context, message = _adapter()

        await picture_handlers.pic_command(update, context)

        service.request_free_picture.assert_awaited_once()
        message.reply_photo.assert_awaited_once()
        assert "免费预览" in message.reply_photo.await_args.kwargs["caption"]

    asyncio.run(scenario())


def test_picture_help_contains_no_paid_hd_entry() -> None:
    """@brief `/pic help` 不再宣传付费高清能力 / `/pic help` no longer advertises paid HD capability.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 调用帮助分支 / Exercise the help branch.

        @return None / None.
        """

        update, context, message = _adapter(args=("help",))
        await picture_handlers.pic_command(update, context)

        message.reply_text.assert_awaited_once_with(picture_handlers.PICTURE_FREE_HELP)
        assert "`/pic hd`" not in picture_handlers.PICTURE_FREE_HELP
        assert "按钮" not in picture_handlers.PICTURE_FREE_HELP

    asyncio.run(scenario())
