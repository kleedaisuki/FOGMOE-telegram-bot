"""Telegram 音乐 handler 的能力隔离与 callback 语义测试 / Capability isolation and callback semantics for Telegram music handlers."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fogmoe_bot.application.media.music_service import MusicPage
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)
from fogmoe_bot.presentation.telegram.media_handlers import music as music_handlers


def _page() -> MusicPage:
    session = MusicSearchSession(
        search_id=MusicSearchId("a" * 32),
        requester_id=UserId(42),
        query="song <name>",
        platform=MusicPlatform.NETEASE,
        tracks=(MusicTrack("1", "song", "artist", "album", MusicPlatform.NETEASE),),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    return MusicPage(session=session, page=1, total_pages=1, tracks=session.tracks)


class _MusicServiceFake:
    def __init__(self) -> None:
        self.search_kwargs: dict[str, object] = {}
        self.switch_kwargs: dict[str, object] = {}

    async def search(self, **kwargs: object) -> MusicPage:
        self.search_kwargs = dict(kwargs)
        return _page()

    async def switch_platform(self, **kwargs: object) -> MusicPage:
        self.switch_kwargs = dict(kwargs)
        return _page()


def test_music_command_uses_its_narrow_service_and_opaque_keyboard(monkeypatch) -> None:
    async def scenario() -> None:
        service = _MusicServiceFake()
        monkeypatch.setattr(music_handlers, "_service", lambda context: service)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42, username="klee"),
            effective_message=message,
        )
        context = SimpleNamespace(args=("song", "<name>"))

        await music_handlers.music_command(update, context)

        assert service.search_kwargs == {
            "user_id": UserId(42),
            "query": "song <name>",
        }
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        assert "song &lt;name&gt;" in text
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard for button in row
        ]
        assert callbacks
        assert all(callback is None or len(callback) <= 64 for callback in callbacks)

    asyncio.run(scenario())


def test_invalid_music_callback_is_rejected_before_capability_lookup(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        def unexpected_lookup(context: object) -> object:
            raise AssertionError("unexpected capability lookup")

        monkeypatch.setattr(music_handlers, "_service", unexpected_lookup)
        query = SimpleNamespace(
            data="music_invalid",
            message=None,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            callback_query=query,
        )

        await music_handlers.music_callback(update, SimpleNamespace())

        query.answer.assert_awaited_once_with("无效的回调数据", show_alert=True)

    asyncio.run(scenario())


def test_music_platform_callback_decodes_only_opaque_session_semantics(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        service = _MusicServiceFake()
        monkeypatch.setattr(music_handlers, "_service", lambda context: service)
        query = SimpleNamespace(
            data=f"music_s_{'a' * 32}_qq_1",
            message=None,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            callback_query=query,
        )

        await music_handlers.music_callback(update, SimpleNamespace())

        assert service.switch_kwargs == {
            "user_id": UserId(42),
            "search_id": MusicSearchId("a" * 32),
            "platform": MusicPlatform.QQ,
            "page": 1,
        }
        query.answer.assert_awaited_once_with()

    asyncio.run(scenario())
