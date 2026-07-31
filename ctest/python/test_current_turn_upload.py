"""@brief 当前 Turn Telegram Document 引用与内存下载的 CTest / CTest for current-Turn Telegram Document references and in-memory download."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram.error import NetworkError

from fogmoe_bot.application.assistant.current_turn_upload import (
    CURRENT_TURN_UPLOAD_MAX_BYTES,
    CurrentTurnUploadDownloadError,
    CurrentTurnUploadKind,
    CurrentTurnUploadReference,
    CurrentTurnUploadSource,
    CurrentTurnUploadTooLargeError,
    CurrentTurnUploadTransportError,
)
from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
)
from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantAccountContext,
)
from fogmoe_bot.domain.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.identity import TurnId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.infrastructure.telegram.current_turn_upload import (
    TelegramCurrentTurnUploadSource,
)
from fogmoe_bot.presentation.telegram.assistant_update_parser import (
    parse_telegram_assistant_update,
)


_NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定 durable ingress 时间 / Fixed durable-ingress timestamp."""


class _HttpTimeouts:
    """@brief 现有 Telegram HTTP 设置的最小替身 / Minimal double for existing Telegram HTTP settings."""

    connect_timeout_seconds = 2.0
    """@brief 连接超时秒数 / Connect timeout in seconds."""
    read_timeout_seconds = 3.0
    """@brief 读取超时秒数 / Read timeout in seconds."""
    write_timeout_seconds = 4.0
    """@brief 写入超时秒数 / Write timeout in seconds."""
    pool_timeout_seconds = 5.0
    """@brief 连接池超时秒数 / Pool-acquisition timeout in seconds."""


class _RemoteFile:
    """@brief 提供已解析 metadata 的 Telegram File 替身 / Telegram File double providing resolved metadata."""

    def __init__(
        self,
        *,
        file_id: str,
        file_unique_id: str,
        file_size: int | None,
        file_path: str | None,
        payload: bytes,
    ) -> None:
        """@brief 保存固定 File 行为 / Store fixed File behavior.

        @param file_id ``get_file`` 返回的 file ID / File ID returned by ``get_file``.
        @param file_unique_id ``get_file`` 返回的稳定 identity / Stable identity returned by ``get_file``.
        @param file_size provider 声明的可选大小 / Optional size declared by the provider.
        @param file_path PTB 解析出的下载 URL / Download URL resolved by PTB.
        @param payload 将由共享 Bot request 返回的内存 bytes / In-memory bytes returned by the shared Bot request.
        """

        self.file_id = file_id
        self.file_unique_id = file_unique_id
        self.file_size = file_size
        self.file_path = file_path
        self.payload = payload


class _Request:
    """@brief 记录 HTTPS 内存读取的 PTB request 替身 / PTB request double recording HTTPS in-memory retrieval."""

    def __init__(self, payload: bytes) -> None:
        """@brief 保存固定下载 bytes / Store fixed download bytes.

        @param payload ``retrieve`` 返回的不可变 bytes / Immutable bytes returned by ``retrieve``.
        """

        self.payload = payload
        self.retrieve_calls: list[dict[str, object]] = []

    async def retrieve(
        self,
        url: str,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> bytes:
        """@brief 记录网络读取并返回固定 bytes / Record network retrieval and return fixed bytes.

        @param url 已验证 HTTPS URL / Validated HTTPS URL.
        @param read_timeout 读取超时 / Read timeout.
        @param write_timeout 写入超时 / Write timeout.
        @param connect_timeout 连接超时 / Connect timeout.
        @param pool_timeout 连接池超时 / Pool-acquisition timeout.
        @return 固定不可变 bytes / Fixed immutable bytes.
        """

        self.retrieve_calls.append(
            {
                "url": url,
                "read_timeout": read_timeout,
                "write_timeout": write_timeout,
                "connect_timeout": connect_timeout,
                "pool_timeout": pool_timeout,
            }
        )
        return self.payload


class _FailingRequest:
    """@brief 模拟含 capability URL 的底层 request 失败 / Simulate a lower-level request failure carrying a capability URL."""

    async def retrieve(
        self,
        url: str,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> bytes:
        """@brief 抛出不应泄露给上层的错误 / Raise an error that must not reach the upper layer.

        @param url 已验证 HTTPS URL / Validated HTTPS URL.
        @param read_timeout 读取超时 / Read timeout.
        @param write_timeout 写入超时 / Write timeout.
        @param connect_timeout 连接超时 / Connect timeout.
        @param pool_timeout 连接池超时 / Pool-acquisition timeout.
        @return 永不返回 / Never returns.
        """

        raise NetworkError(f"request failed for {url}")


class _Bot:
    """@brief 记录 ``get_file`` 调用的窄 Telegram Bot 替身 / Narrow Telegram Bot double recording ``get_file`` calls."""

    def __init__(self, file: _RemoteFile) -> None:
        """@brief 绑定待返回的 File / Bind the File to return.

        @param file 固定 ``get_file`` 结果 / Fixed ``get_file`` result.
        """

        self.file = file
        self.request = _Request(file.payload)
        self.get_file_calls: list[dict[str, object]] = []

    async def get_file(
        self,
        file_id: str,
        *,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> _RemoteFile:
        """@brief 记录 provider preflight / Record provider preflight.

        @param file_id adapter 唯一传入的 file ID / Sole file ID passed by the adapter.
        @param read_timeout 读取超时 / Read timeout.
        @param write_timeout 写入超时 / Write timeout.
        @param connect_timeout 连接超时 / Connect timeout.
        @param pool_timeout 连接池超时 / Pool-acquisition timeout.
        @return 固定 File / Fixed File.
        """

        self.get_file_calls.append(
            {
                "file_id": file_id,
                "read_timeout": read_timeout,
                "write_timeout": write_timeout,
                "connect_timeout": connect_timeout,
                "pool_timeout": pool_timeout,
            }
        )
        return self.file


def _reference(
    *,
    declared_byte_size: int | None = 7,
) -> CurrentTurnUploadReference:
    """@brief 构造已接受 Document 的强类型引用 / Build a strongly typed accepted-Document reference.

    @param declared_byte_size ingress 声明的可选大小 / Optional ingress-declared size.
    @return 可传给 application importer 的引用 / Reference suitable for the application importer.
    """

    return CurrentTurnUploadReference(
        file_id="telegram-file-capability",
        file_unique_id="telegram-stable-identity",
        source_update_id=101,
        source_message_id=7,
        declared_byte_size=declared_byte_size,
        original_file_name="notes.txt",
        mime_type="text/plain",
    )


def _source(file: _RemoteFile) -> tuple[TelegramCurrentTurnUploadSource, _Bot]:
    """@brief 构造 source adapter 与记录 Bot / Build the source adapter and recording Bot.

    @param file 固定 provider File / Fixed provider File.
    @return source 与其 Bot 替身 / Source and its Bot double.
    """

    bot = _Bot(file)
    return (
        TelegramCurrentTurnUploadSource(bot=bot, http=_HttpTimeouts()),
        bot,
    )


def _document_inbound() -> InboundUpdate:
    """@brief 构造最小 durable Telegram Document Update / Build a minimal durable Telegram Document Update.

    @return 可由 Assistant parser 读取的 pending Update / Pending Update readable by the Assistant parser.
    """

    payload = {
        "update_id": 101,
        "message": {
            "message_id": 7,
            "date": 1_893_456_000,
            "chat": {"id": 42, "type": "private"},
            "from": {
                "id": 42,
                "is_bot": False,
                "first_name": "Klee",
                "last_name": "Spark",
                "username": "klee",
            },
            "caption": "please import this",
            "document": {
                "file_id": "telegram-file-capability",
                "file_unique_id": "telegram-stable-identity",
                "file_size": 7,
                "file_name": "notes.txt",
                "mime_type": "text/plain",
            },
        },
    }
    return InboundUpdate.pending(
        update_id=UpdateId(101),
        conversation_id=TelegramConversationAddress(
            chat_type="private",
            chat_id=42,
            user_id=42,
            message_thread_id=None,
        ).conversation_id,
        payload=payload,
        received_at=_NOW,
    )


def _command(
    upload: CurrentTurnUploadReference | None,
) -> DurableAssistantInferenceCommand:
    """@brief 构造最小 durable Assistant command / Build a minimal durable Assistant command.

    @param upload 可选当前 Turn Document 引用 / Optional current-Turn Document reference.
    @return 严格 durable command / Strict durable command.
    """

    return DurableAssistantInferenceCommand(
        schema_version=2,
        conversation_id="assistant-user:42",
        turn_id=str(TurnId.new()),
        delivery_stream_id="telegram:primary:chat:42:thread:0",
        chat_id=42,
        reply_to_message_id=7 if upload is not None else None,
        message_thread_id=None,
        user=DurableAssistantUser(
            user_id=42,
            username="klee",
            display_name="Klee",
            coins=0,
            plan=AccountPlan.FREE,
            permission=0,
        ),
        scope=DurableAssistantScope(
            is_group=False,
            message_id=7 if upload is not None else None,
        ),
        current_turn_upload=upload,
    )


class CurrentTurnUploadTests(unittest.TestCase):
    """@brief durable 引用与 Telegram downloader 的边界合约 / Boundary contracts for durable references and the Telegram downloader."""

    def test_document_reference_flows_to_durable_command_and_old_rows_remain_readable(
        self,
    ) -> None:
        """@brief Document 引用流经 Request/Command，缺字段旧 JSON 仍可读 / Document flows through Request/Command while old JSON without the field remains readable.

        @return None / None.
        """

        inbound = _document_inbound()
        request = parse_telegram_assistant_update(inbound).to_request(inbound)
        reference = request.current_turn_upload
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.file_id, "telegram-file-capability")
        self.assertEqual(reference.file_unique_id, "telegram-stable-identity")
        self.assertEqual(reference.source_update_id, 101)
        self.assertEqual(reference.source_message_id, 7)
        self.assertEqual(reference.original_file_name, "notes.txt")

        accepted = request.to_accept_turn(
            AssistantAccountContext(
                coins=0,
                plan=AccountPlan.FREE,
                permission=0,
                profile=None,
                personal_info="",
                diary_exists=False,
            ),
            accepted_at=_NOW + timedelta(seconds=1),
        )
        persisted = accepted.inference_request
        self.assertEqual(
            persisted["current_turn_upload"],
            {
                "kind": "document",
                "file_id": "telegram-file-capability",
                "file_unique_id": "telegram-stable-identity",
                "source_update_id": 101,
                "source_message_id": 7,
                "declared_byte_size": 7,
                "original_file_name": "notes.txt",
                "mime_type": "text/plain",
            },
        )
        restored = DurableAssistantInferenceCommand.from_json(persisted)
        self.assertEqual(restored.current_turn_upload, reference)

        # 早期已落库的 Document 引用没有 kind 字段；读取时必须收敛为原有 Document
        # 语义，而不是让部署升级后遗留 activity 解析失败。/ Early persisted Document
        # references had no kind field; reading them must converge to the original Document
        # semantics rather than making an existing activity fail after deployment.
        legacy_kind_row = dict(persisted)
        legacy_kind_upload = dict(legacy_kind_row["current_turn_upload"])
        legacy_kind_upload.pop("kind")
        legacy_kind_row["current_turn_upload"] = legacy_kind_upload
        restored_legacy_kind = DurableAssistantInferenceCommand.from_json(
            legacy_kind_row
        )
        assert restored_legacy_kind.current_turn_upload is not None
        self.assertIs(
            restored_legacy_kind.current_turn_upload.kind,
            CurrentTurnUploadKind.DOCUMENT,
        )

        old_row = dict(persisted)
        old_row.pop("current_turn_upload")
        self.assertIsNone(
            DurableAssistantInferenceCommand.from_json(old_row).current_turn_upload
        )
        self.assertNotIn("current_turn_upload", _command(None).to_json())

    def test_oversized_ingress_reference_cannot_enter_durable_command(self) -> None:
        """@brief ingress 可先反馈超限，但 durable command 不能持有它 / Ingress may report an oversized upload first, but a durable command cannot carry it.

        @return None / None.
        """

        oversized = _reference(
            declared_byte_size=CURRENT_TURN_UPLOAD_MAX_BYTES + 1
        )
        with self.assertRaises(ValueError):
            _command(oversized)

    def test_downloader_uses_only_durable_reference_and_returns_verified_immutable_bytes(
        self,
    ) -> None:
        """@brief source 只用引用 file ID，并返回 SHA-256 核验的不可变 bytes / Source uses only the reference file ID and returns SHA-256-verified immutable bytes.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 执行一次受限下载 / Execute one bounded download.

            @return None / None.
            """

            raw = b"payload"
            file = _RemoteFile(
                file_id="telegram-file-capability",
                file_unique_id="telegram-stable-identity",
                file_size=len(raw),
                file_path="https://api.telegram.org/file/bot-redacted/documents/x",
                payload=raw,
            )
            source, bot = _source(file)
            result = await source.download(_reference(declared_byte_size=len(raw)))

            self.assertIs(type(result.content), bytes)
            self.assertEqual(result.content, b"payload")
            self.assertEqual(result.byte_size, 7)
            self.assertEqual(result.sha256, hashlib.sha256(b"payload").hexdigest())
            self.assertEqual(result.original_file_name, "notes.txt")
            self.assertEqual(result.mime_type, "text/plain")
            self.assertEqual(
                bot.get_file_calls,
                [
                    {
                        "file_id": "telegram-file-capability",
                        "read_timeout": 3.0,
                        "write_timeout": 4.0,
                        "connect_timeout": 2.0,
                        "pool_timeout": 5.0,
                    }
                ],
            )
            self.assertEqual(
                bot.request.retrieve_calls,
                [
                    {
                        "url": "https://api.telegram.org/file/bot-redacted/documents/x",
                        "read_timeout": 3.0,
                        "write_timeout": 4.0,
                        "connect_timeout": 2.0,
                        "pool_timeout": 5.0,
                    }
                ],
            )
            self.assertFalse(hasattr(result, "host_path"))
            self.assertFalse(hasattr(result, "workspace_path"))

        asyncio.run(scenario())

    def test_downloader_preflights_resolved_size_before_download(self) -> None:
        """@brief ``get_file`` 超限或未知大小在下载前拒绝 / An over-limit or unknown ``get_file`` size is rejected before downloading.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 测试 provider-side 大小门 / Test the provider-side size gate.

            @return None / None.
            """

            for size in (CURRENT_TURN_UPLOAD_MAX_BYTES + 1, None):
                with self.subTest(file_size=size):
                    file = _RemoteFile(
                        file_id="telegram-file-capability",
                        file_unique_id="telegram-stable-identity",
                        file_size=size,
                        file_path="https://api.telegram.org/file/bot-redacted/documents/x",
                        payload=b"not-read",
                    )
                    source, bot = _source(file)
                    error = (
                        CurrentTurnUploadTooLargeError
                        if size is not None
                        else CurrentTurnUploadDownloadError
                    )
                    with self.assertRaises(error):
                        await source.download(_reference())
                    self.assertEqual(bot.request.retrieve_calls, [])

        asyncio.run(scenario())

    def test_downloader_rechecks_actual_size_and_rejects_local_file_mode(self) -> None:
        """@brief adapter 对实际 bytes 再验大小，并拒绝 PTB local-file 分支 / Adapter rechecks actual bytes and rejects PTB local-file mode.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 覆盖 post-download 和 local-path 两条拒绝路径 / Cover post-download and local-path rejection paths.

            @return None / None.
            """

            oversized_raw = b"\x00" * (CURRENT_TURN_UPLOAD_MAX_BYTES + 1)
            oversized_file = _RemoteFile(
                file_id="telegram-file-capability",
                file_unique_id="telegram-stable-identity",
                file_size=1,
                file_path="https://api.telegram.org/file/bot-redacted/documents/x",
                payload=oversized_raw,
            )
            oversized_source, oversized_bot = _source(oversized_file)
            with self.assertRaises(CurrentTurnUploadTooLargeError):
                await oversized_source.download(_reference(declared_byte_size=1))
            self.assertEqual(len(oversized_bot.request.retrieve_calls), 1)

            local_file = _RemoteFile(
                file_id="telegram-file-capability",
                file_unique_id="telegram-stable-identity",
                file_size=1,
                file_path="/host/private/file",
                payload=b"x",
            )
            local_source, local_bot = _source(local_file)
            with self.assertRaises(CurrentTurnUploadDownloadError):
                await local_source.download(_reference(declared_byte_size=1))
            self.assertEqual(local_bot.request.retrieve_calls, [])

        asyncio.run(scenario())

    def test_downloader_maps_known_network_failure_without_leaking_capability_url(self) -> None:
        """@brief 已知网络错误可重试且不得泄露下载 capability URL / A known network error is retryable and must not expose the download capability URL.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 触发并检查已净化的下载错误 / Trigger and inspect the sanitized download error.

            @return None / None.
            """

            file = _RemoteFile(
                file_id="telegram-file-capability",
                file_unique_id="telegram-stable-identity",
                file_size=1,
                file_path="https://api.telegram.org/file/bot-secret/documents/x",
                payload=b"x",
            )
            source, bot = _source(file)
            bot.request = _FailingRequest()
            with self.assertRaises(CurrentTurnUploadTransportError) as captured:
                await source.download(_reference(declared_byte_size=1))
            self.assertNotIn("bot-secret", str(captured.exception))
            self.assertIsNone(captured.exception.__cause__)
            self.assertIsNone(captured.exception.__context__)

        asyncio.run(scenario())

    def test_source_api_is_reference_only_and_never_contains_file_write_or_execution_paths(
        self,
    ) -> None:
        """@brief downloader API 没有任意 file ID 参数，也不含落盘或执行 API / Downloader API has no arbitrary file-ID parameter nor file-write or execution APIs.

        @return None / None.
        """

        self.assertEqual(
            tuple(inspect.signature(CurrentTurnUploadSource.download).parameters),
            ("self", "reference"),
        )
        source = Path(
            "src/fogmoe_bot/infrastructure/telegram/current_turn_upload.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "ToolExecutionContext",
            "download_to_drive",
            "tempfile",
            "subprocess",
            "os.open",
            "open(",
            "exec(",
            "compile(",
            "bot_token",
            "ApplicationBuilder",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
