"""@brief Telegram Document durable ingress 的 CTest / CTest for Telegram Document durable ingress."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fogmoe_bot.application.conversation.assistant_ingress import (
    ASSISTANT_MEDIA_LIMIT_BYTES,
    AssistantIngressCoordinator,
    AssistantTurnAccepted,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.application.assistant.current_turn_upload import (
    CurrentTurnUploadKind,
    workspace_attachment_file_path,
)
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
from fogmoe_bot.domain.conversation.identity import TurnId, TurnSource, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.workspace.attachment import pending_workspace_attachment_marker
from fogmoe_bot.presentation.telegram.assistant_primary_route import (
    TelegramAssistantPrimaryRoute,
)
from fogmoe_bot.presentation.telegram.assistant_update_models import (
    MalformedTelegramAssistantUpdate,
    TelegramAssistantContentKind,
)
from fogmoe_bot.presentation.telegram.assistant_update_parser import (
    looks_like_assistant_candidate,
    parse_telegram_assistant_update,
)


_NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定的 durable Update 接收时刻 / Fixed durable-Update receipt instant."""


class _ManualClock:
    """@brief 固定 Assistant acceptance 时钟 / Fixed Assistant-acceptance clock."""

    def now(self) -> datetime:
        """@brief 返回稳定 acceptance 时刻 / Return a stable acceptance instant.

        @return 晚于 Update 接收一秒的 UTC 时刻 / UTC instant one second after Update receipt.
        """

        return _NOW + timedelta(seconds=1)


class _RecordingAcceptance:
    """@brief 记录 acceptance 调用的最小端口替身 / Minimal acceptance-port double recording calls."""

    def __init__(self) -> None:
        """@brief 初始化空调用列表 / Initialize an empty call list."""

        self.calls: list[object] = []

    async def accept(
        self, request: object, *, accepted_at: datetime
    ) -> AssistantTurnAccepted:
        """@brief 记录一次 acceptance / Record one acceptance.

        @param request 已验证的 Assistant 请求 / Validated Assistant request.
        @param accepted_at acceptance 时间 / Acceptance time.
        @return 可重放的成功结果 / Replayable successful result.
        """

        self.calls.append((request, accepted_at))
        return AssistantTurnAccepted(acceptance=None, replayed=True)


class _RecordingFeedback:
    """@brief 记录反馈 outbox 请求的最小替身 / Minimal double recording feedback-outbox commands."""

    def __init__(self) -> None:
        """@brief 初始化按幂等键索引的记录 / Initialize records indexed by idempotency key."""

        self.commands: dict[str, StandaloneOutboundCommand] = {}

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 按幂等键记录一个 feedback 请求 / Record one feedback request by idempotency key.

        @param command 待记录的 outbox 命令 / Outbox command to record.
        @return None / None.
        """

        self.commands.setdefault(command.idempotency_key, command)


def _document(
    *,
    file_name: str | None = "research.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int | None = 4_096,
) -> dict[str, object]:
    """@brief 构造 PTB durable JSON 形状的 Document / Build a Document in PTB durable-JSON shape.

    @param file_name 可选原始文件名 / Optional original filename.
    @param mime_type 可选 Telegram MIME type / Optional Telegram MIME type.
    @param file_size 可选声明大小 / Optional declared size.
    @return Document JSON object / Document JSON object.
    """

    document: dict[str, object] = {
        "file_id": "document-file-id",
        "file_unique_id": "document-unique-id",
    }
    if file_name is not None:
        document["file_name"] = file_name
    if mime_type is not None:
        document["mime_type"] = mime_type
    if file_size is not None:
        document["file_size"] = file_size
    return document


def _message_payload(
    *,
    update_id: int = 100,
    chat_id: int = 42,
    chat_type: str = "private",
    document: dict[str, object] | None = None,
    photo: dict[str, object] | None = None,
    sticker: dict[str, object] | None = None,
    voice: dict[str, object] | None = None,
    audio: dict[str, object] | None = None,
    video: dict[str, object] | None = None,
    animation: dict[str, object] | None = None,
    video_note: dict[str, object] | None = None,
    caption: str | None = "please inspect this",
) -> dict[str, object]:
    """@brief 构造最小 durable Assistant message / Build a minimal durable Assistant message.

    @param update_id Telegram Update ID / Telegram Update ID.
    @param chat_id 私聊或群聊 chat ID / Private or group chat ID.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param document 可选 Document / Optional Document.
    @param photo 可选 PhotoSize / Optional PhotoSize.
    @param sticker 可选 Sticker / Optional Sticker.
    @param voice 可选 Voice / Optional Voice.
    @param audio 可选 Audio / Optional Audio.
    @param video 可选 Video / Optional Video.
    @param animation 可选 Animation / Optional Animation.
    @param video_note 可选 VideoNote / Optional VideoNote.
    @param caption 媒体 caption / Media caption.
    @return PTB ``to_json`` 形状的 Update / Update in PTB ``to_json`` shape.
    """

    message: dict[str, object] = {
        "message_id": 7,
        "date": 1_893_456_000,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {
            "id": 42,
            "is_bot": False,
            "first_name": "Klee",
            "last_name": "Spark",
            "username": "klee",
        },
    }
    candidates = {
        "document": document,
        "photo": photo,
        "sticker": sticker,
        "voice": voice,
        "audio": audio,
        "video": video,
        "animation": animation,
        "video_note": video_note,
    }
    if sum(candidate is not None for candidate in candidates.values()) != 1:
        raise ValueError("test payload requires exactly one media candidate")
    if document is not None:
        message["document"] = document
    elif photo is not None:
        message["photo"] = [photo]
    elif sticker is not None:
        message["sticker"] = sticker
    else:
        for kind, candidate in candidates.items():
            if candidate is not None:
                message[kind] = candidate
                break
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def _inbound(payload: dict[str, object]) -> InboundUpdate:
    """@brief 将严格 payload 包装为 durable inbox Update / Wrap a strict payload in a durable inbox Update.

    @param payload PTB durable JSON shape / PTB durable JSON shape.
    @return 供 parser/route 使用的 inbox Update / Inbox Update for the parser/route.
    """

    message = payload["message"]
    if not isinstance(message, dict):
        raise AssertionError("test message must be an object")
    chat = message["chat"]
    sender = message["from"]
    update_id = payload["update_id"]
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        raise AssertionError("test identity objects must be objects")
    chat_id = chat["id"]
    chat_type = chat["type"]
    user_id = sender["id"]
    if not isinstance(update_id, int) or not isinstance(chat_id, int):
        raise AssertionError("test identifiers must be integers")
    if not isinstance(chat_type, str) or not isinstance(user_id, int):
        raise AssertionError("test chat type/user ID are invalid")
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=TelegramConversationAddress(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            message_thread_id=None,
        ).conversation_id,
        payload=payload,
        received_at=_NOW,
    )


def _route() -> tuple[
    TelegramAssistantPrimaryRoute, _RecordingAcceptance, _RecordingFeedback
]:
    """@brief 构造 document ingress route 及记录端口 / Build the document-ingress route and recording ports.

    @return route、acceptance 和 feedback doubles / Route, acceptance, and feedback doubles.
    """

    acceptance = _RecordingAcceptance()
    feedback = _RecordingFeedback()
    return (
        TelegramAssistantPrimaryRoute(
            coordinator=AssistantIngressCoordinator(
                acceptance=acceptance,  # type: ignore[arg-type]
                feedback=feedback,
                clock=_ManualClock(),
            ),
            bot_user_id=999,
            bot_username="FogMoeBot",
        ),
        acceptance,
        feedback,
    )


class TelegramWorkspaceDocumentIngressTests(unittest.TestCase):
    """@brief Document 引用的 parser/model/route 合约 / Parser/model/route contracts for Document references."""

    def test_document_reference_is_durable_metadata_without_bytes_or_host_path(
        self,
    ) -> None:
        """@brief Document 仅持久化可重放元数据 / A Document persists replayable metadata only.

        @return None / None.
        """

        inbound = _inbound(_message_payload(document=_document()))
        parsed = parse_telegram_assistant_update(inbound)

        self.assertIs(parsed.content_kind, TelegramAssistantContentKind.DOCUMENT)
        self.assertEqual(parsed.text, "please inspect this")
        self.assertIsNotNone(parsed.media)
        assert parsed.media is not None
        self.assertEqual(parsed.media.file_id, "document-file-id")
        self.assertEqual(parsed.media.file_unique_id, "document-unique-id")
        self.assertEqual(parsed.media.file_name, "research.pdf")
        self.assertEqual(parsed.media.mime_type, "application/pdf")
        self.assertEqual(parsed.media.file_size, 4_096)
        self.assertIsNone(parsed.media.width)
        self.assertIsNone(parsed.media.height)

        reference = parsed.to_request(inbound).user_content["media"]
        self.assertEqual(
            reference,
            {
                "kind": "document",
                "file_id": "document-file-id",
                "file_unique_id": "document-unique-id",
                "file_size": 4_096,
                "width": None,
                "height": None,
                "mime_type": "application/pdf",
                "emoji": None,
                "max_download_bytes": ASSISTANT_MEDIA_LIMIT_BYTES,
                "file_name": "research.pdf",
            },
        )
        assert isinstance(reference, dict)
        self.assertTrue(
            {
                "bytes",
                "content",
                "host_path",
                "workspace_path",
                "download_url",
            }.isdisjoint(reference)
        )

    def test_document_optional_filename_and_mime_stay_metadata_only(self) -> None:
        """@brief Telegram 可省略的 filename/MIME 原样保留为 None / Optional Telegram filename/MIME remain None.

        @return None / None.
        """

        inbound = _inbound(
            _message_payload(
                document=_document(file_name=None, mime_type=None, file_size=None),
                caption=None,
            )
        )
        parsed = parse_telegram_assistant_update(inbound)
        reference = parsed.to_request(inbound).user_content["media"]

        self.assertEqual(parsed.text, "[document]")
        self.assertIsNotNone(parsed.media)
        assert parsed.media is not None
        self.assertIsNone(parsed.media.file_name)
        self.assertIsNone(parsed.media.mime_type)
        self.assertIsNone(parsed.media.file_size)
        assert isinstance(reference, dict)
        self.assertEqual(reference["file_name"], None)
        self.assertEqual(reference["mime_type"], None)
        self.assertEqual(reference["file_size"], None)

    def test_document_route_accepts_at_limit_and_rejects_declared_oversize(
        self,
    ) -> None:
        """@brief route 将 8 MiB Document 接受，声明超限则仅反馈 / Route accepts a 8 MiB Document and only feeds back declared oversize.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行两个 route 调用 / Run two route invocations.

            @return None / None.
            """

            route, acceptance, feedback = _route()
            at_limit = _inbound(
                _message_payload(
                    update_id=101,
                    document=_document(file_size=ASSISTANT_MEDIA_LIMIT_BYTES),
                )
            )
            oversized = _inbound(
                _message_payload(
                    update_id=102,
                    document=_document(file_size=ASSISTANT_MEDIA_LIMIT_BYTES + 1),
                )
            )

            self.assertTrue(route.matches(at_limit))
            self.assertTrue(route.matches(oversized))
            await (await route.operation(at_limit)).call()
            await (await route.operation(oversized)).call()

            self.assertEqual(len(acceptance.calls), 1)
            self.assertEqual(
                list(feedback.commands),
                ["update:102:assistant-feedback:media_too_large"],
            )

        asyncio.run(scenario())

    def test_document_malformed_candidate_is_quarantined_and_not_silently_ignored(
        self,
    ) -> None:
        """@brief 畸形 Document 仍是 Assistant candidate 并触发永久 parser 错误 / A malformed Document remains an Assistant candidate and causes a permanent parser error.

        @return None / None.
        """

        payload = _message_payload(document=_document())
        message = payload["message"]
        assert isinstance(message, dict)
        document = message["document"]
        assert isinstance(document, dict)
        document["file_id"] = 7
        inbound = _inbound(payload)
        route, _acceptance, _feedback = _route()

        self.assertTrue(looks_like_assistant_candidate(payload))
        self.assertTrue(route.matches(inbound))
        with self.assertRaises(MalformedTelegramAssistantUpdate):
            parse_telegram_assistant_update(inbound)

    def test_every_attachment_kind_gets_a_durable_upload_and_model_placeholder(
        self,
    ) -> None:
        """@brief 八类 Telegram 附件都走同一预导入边界 / All eight Telegram attachment kinds use the same pre-import boundary.

        @return None / None.
        """

        photo = {
            "file_id": "photo-file-id",
            "file_unique_id": "photo-unique-id",
            "file_size": 123,
            "width": 640,
            "height": 480,
        }
        sticker = {
            "file_id": "sticker-file-id",
            "file_unique_id": "sticker-unique-id",
            "file_size": 456,
            "width": 512,
            "height": 512,
            "is_animated": False,
            "is_video": False,
            "emoji": "✨",
        }
        voice = {
            "file_id": "voice-file-id",
            "file_unique_id": "voice-unique-id",
            "file_size": 321,
            "mime_type": "audio/ogg",
        }
        audio = {
            "file_id": "audio-file-id",
            "file_unique_id": "audio-unique-id",
            "file_size": 654,
            "mime_type": "audio/mpeg",
            "file_name": "song.mp3",
        }
        video = {
            "file_id": "video-file-id",
            "file_unique_id": "video-unique-id",
            "file_size": 987,
            "mime_type": "video/mp4",
            "file_name": "clip.mp4",
            "width": 1280,
            "height": 720,
        }
        animation = {
            "file_id": "animation-file-id",
            "file_unique_id": "animation-unique-id",
            "file_size": 246,
            "mime_type": "video/mp4",
            "file_name": "loop.mp4",
            "width": 320,
            "height": 240,
        }
        video_note = {
            "file_id": "video-note-file-id",
            "file_unique_id": "video-note-unique-id",
            "file_size": 135,
            "length": 240,
        }
        cases = (
            (
                "photo",
                _inbound(_message_payload(update_id=201, photo=photo)),
                CurrentTurnUploadKind.PHOTO,
                "photo-file-id",
                "photo-unique-id",
            ),
            (
                "sticker",
                _inbound(_message_payload(update_id=202, sticker=sticker)),
                CurrentTurnUploadKind.STICKER,
                "sticker-file-id",
                "sticker-unique-id",
            ),
            (
                "document",
                _inbound(_message_payload(update_id=203, document=_document())),
                CurrentTurnUploadKind.DOCUMENT,
                "document-file-id",
                "document-unique-id",
            ),
            (
                "voice",
                _inbound(_message_payload(update_id=204, voice=voice)),
                CurrentTurnUploadKind.VOICE,
                "voice-file-id",
                "voice-unique-id",
            ),
            (
                "audio",
                _inbound(_message_payload(update_id=205, audio=audio)),
                CurrentTurnUploadKind.AUDIO,
                "audio-file-id",
                "audio-unique-id",
            ),
            (
                "video",
                _inbound(_message_payload(update_id=206, video=video)),
                CurrentTurnUploadKind.VIDEO,
                "video-file-id",
                "video-unique-id",
            ),
            (
                "animation",
                _inbound(_message_payload(update_id=207, animation=animation)),
                CurrentTurnUploadKind.ANIMATION,
                "animation-file-id",
                "animation-unique-id",
            ),
            (
                "video_note",
                _inbound(_message_payload(update_id=208, video_note=video_note)),
                CurrentTurnUploadKind.VIDEO_NOTE,
                "video-note-file-id",
                "video-note-unique-id",
            ),
        )
        for name, inbound, expected_kind, file_id, unique_id in cases:
            with self.subTest(kind=name):
                request = parse_telegram_assistant_update(inbound).to_request(inbound)
                upload = request.current_turn_upload
                self.assertIsNotNone(upload)
                assert upload is not None
                self.assertIs(upload.kind, expected_kind)
                self.assertEqual(upload.file_id, file_id)
                self.assertEqual(upload.file_unique_id, unique_id)
                model_message = CanonicalMessage.from_json(
                    request.user_content["model_message"]
                )
                expected_path = workspace_attachment_file_path(
                    turn_id=TurnId.for_source(TurnSource.telegram(inbound.update_id)),
                    reference=upload,
                )
                self.assertEqual(
                    model_message.text,
                    f'<workspace_file path="{expected_path}" />',
                )
                self.assertEqual(request.user_content["text"], model_message.text)
                self.assertEqual(
                    request.user_content["workspace_attachment"],
                    pending_workspace_attachment_marker(),
                )
                for forbidden in (
                    "please inspect this",
                    file_id,
                    unique_id,
                    "research.pdf",
                    "application/pdf",
                ):
                    self.assertNotIn(forbidden, model_message.text)

        photo_reference = (
            parse_telegram_assistant_update(_inbound(_message_payload(photo=photo)))
            .to_request(_inbound(_message_payload(photo=photo)))
            .user_content["media"]
        )
        sticker_reference = (
            parse_telegram_assistant_update(_inbound(_message_payload(sticker=sticker)))
            .to_request(_inbound(_message_payload(sticker=sticker)))
            .user_content["media"]
        )

        assert isinstance(photo_reference, dict)
        assert isinstance(sticker_reference, dict)
        self.assertNotIn("file_name", photo_reference)
        self.assertNotIn("file_name", sticker_reference)
        self.assertEqual(photo_reference["width"], 640)
        self.assertEqual(photo_reference["height"], 480)
        self.assertEqual(sticker_reference["width"], 512)
        self.assertEqual(sticker_reference["height"], 512)

    def test_group_attachment_caption_can_route_but_never_becomes_model_text(
        self,
    ) -> None:
        """@brief 群附件 caption 只用于路由，接受后仍只暴露 Workspace 占位符 / A group attachment caption routes only; after acceptance it still exposes only a Workspace placeholder.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行群图片 mention 的完整 route/acceptance 路径 / Run the full route/acceptance path for a group-photo mention.

            @return None / None.
            """

            route, acceptance, _feedback = _route()
            caption = "@FogMoeBot inspect this but never send the caption to the model"
            inbound = _inbound(
                _message_payload(
                    update_id=301,
                    chat_id=-1001,
                    chat_type="supergroup",
                    photo={
                        "file_id": "group-photo-id",
                        "file_unique_id": "group-photo-unique",
                        "file_size": 123,
                        "width": 640,
                        "height": 480,
                    },
                    caption=caption,
                )
            )

            self.assertTrue(route.matches(inbound))
            await (await route.operation(inbound)).call()
            self.assertEqual(len(acceptance.calls), 1)
            request, _accepted_at = acceptance.calls[0]
            user_content = request.user_content
            model_message = CanonicalMessage.from_json(user_content["model_message"])
            self.assertEqual(user_content["text"], model_message.text)
            self.assertTrue(model_message.text.startswith('<workspace_file path="'))
            self.assertNotIn(caption, model_message.text)
            self.assertNotIn("group-photo-id", model_message.text)

        asyncio.run(scenario())

    def test_ingress_sources_do_not_download_or_expose_host_paths(self) -> None:
        """@brief parser/model/route 仅处理引用，绝不触发下载或 host 路径 / Parser/model/route only handle references and never trigger downloads or host paths.

        @return None / None.
        """

        root = Path(__file__).resolve().parents[2]
        sources = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (
                "src/fogmoe_bot/presentation/telegram/assistant_update_models.py",
                "src/fogmoe_bot/presentation/telegram/assistant_update_parser.py",
                "src/fogmoe_bot/presentation/telegram/assistant_primary_route.py",
            )
        )
        for forbidden in (
            "from telegram",
            "import telegram",
            "get_file(",
            "download_to",
            "download_as_bytearray",
            "subprocess",
            "workspace_path",
            "host_path",
        ):
            self.assertNotIn(forbidden, sources)


if __name__ == "__main__":
    unittest.main()
