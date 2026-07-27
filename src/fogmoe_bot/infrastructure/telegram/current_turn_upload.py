"""@brief Telegram 当前 Turn 附件的内存下载 adapter / In-memory download adapter for a Telegram current-Turn attachment.

adapter 只接收进程已有的 Telegram Bot；它不会读取 token、创建另一套 HTTP client、写入
临时文件，或把文件名解释为任何路径。/ The adapter accepts only the process's existing
Telegram Bot; it neither reads a token, creates another HTTP client, writes a temporary file, nor
interprets a filename as any kind of path.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from telegram.error import NetworkError

from fogmoe_bot.application.assistant.current_turn_upload import (
    CURRENT_TURN_UPLOAD_MAX_BYTES,
    CurrentTurnUploadDownloadError,
    CurrentTurnUploadIntegrityError,
    CurrentTurnUploadReference,
    CurrentTurnUploadSource,
    CurrentTurnUploadTooLargeError,
    CurrentTurnUploadTransportError,
    DownloadedCurrentTurnUpload,
)


class TelegramAttachmentFile(Protocol):
    """@brief 本 adapter 需要的 Telegram File 窄表面 / Narrow Telegram File surface required by this adapter."""

    file_id: str
    """@brief 已解析的 Telegram file ID / Resolved Telegram file ID."""
    file_unique_id: str
    """@brief 已解析的稳定 Telegram file identity / Resolved stable Telegram file identity."""
    file_size: int | None
    """@brief provider 报告的可选字节数 / Optional byte size reported by the provider."""
    file_path: str | None
    """@brief PTB 解析后的下载 URL；仅用于受限网络读取 / PTB-resolved download URL, used only for bounded network retrieval."""


class TelegramFileRequest(Protocol):
    """@brief 本 adapter 需要的已初始化 PTB request 窄表面 / Narrow initialized PTB request surface required by this adapter."""

    async def retrieve(
        self,
        url: str,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> bytes:
        """@brief 以现有 Bot request 读取 HTTPS 文件 URL / Retrieve an HTTPS file URL through the existing Bot request.

        @param url ``get_file`` 解析出的 HTTPS URL / HTTPS URL resolved by ``get_file``.
        @param read_timeout 读取超时秒数 / Read timeout in seconds.
        @param write_timeout 写入超时秒数 / Write timeout in seconds.
        @param connect_timeout 连接超时秒数 / Connect timeout in seconds.
        @param pool_timeout 连接池超时秒数 / Pool-acquisition timeout in seconds.
        @return 不可变内存 bytes / Immutable in-memory bytes.
        """

        ...


class TelegramAttachmentBot(Protocol):
    """@brief 本 adapter 需要的已初始化 Telegram Bot 窄表面 / Narrow initialized Telegram Bot surface required by this adapter."""

    @property
    def request(self) -> TelegramFileRequest:
        """@brief 返回与 Bot 共享的已初始化 PTB request / Return the initialized PTB request shared with the Bot.

        @return 已初始化 request 窄表面 / Initialized narrow request surface.
        """

        ...

    async def get_file(
        self,
        file_id: str,
        *,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        connect_timeout: float | None = None,
        pool_timeout: float | None = None,
    ) -> TelegramAttachmentFile:
        """@brief 解析一个 Telegram file ID / Resolve one Telegram file ID.

        @param file_id durable 引用已授权的 Telegram file ID / Telegram file ID authorized by the durable reference.
        @param read_timeout 读取超时秒数 / Read timeout in seconds.
        @param write_timeout 写入超时秒数 / Write timeout in seconds.
        @param connect_timeout 连接超时秒数 / Connect timeout in seconds.
        @param pool_timeout 连接池超时秒数 / Pool-acquisition timeout in seconds.
        @return 解析后的 File 窄表面 / Resolved narrow File surface.
        """

        ...


class TelegramHttpTimeoutSettings(Protocol):
    """@brief 复用现有 Telegram HTTP 设置的结构化端口 / Structural port reusing existing Telegram HTTP settings."""

    connect_timeout_seconds: float
    """@brief 连接超时秒数 / Connect timeout in seconds."""
    read_timeout_seconds: float
    """@brief 读取超时秒数 / Read timeout in seconds."""
    write_timeout_seconds: float
    """@brief 写入超时秒数 / Write timeout in seconds."""
    pool_timeout_seconds: float
    """@brief 连接池超时秒数 / Pool-acquisition timeout in seconds."""


@dataclass(frozen=True, slots=True)
class _TelegramDownloadTimeouts:
    """@brief 已校验的 Telegram 下载超时 / Validated Telegram download timeouts.

    @param connect 连接超时秒数 / Connect timeout in seconds.
    @param read 读取超时秒数 / Read timeout in seconds.
    @param write 写入超时秒数 / Write timeout in seconds.
    @param pool 连接池超时秒数 / Pool-acquisition timeout in seconds.
    """

    connect: float
    read: float
    write: float
    pool: float


class TelegramCurrentTurnUploadSource(CurrentTurnUploadSource):
    """@brief 复用进程 Bot 下载当前 Turn 附件 / Reuse the process Bot to download a current-Turn attachment.

    @note PTB 22 的 ``BaseRequest.retrieve`` 在 client 收到完整响应后才返回 bytes，因此
        这里的 ``file_size`` preflight 是 provider-side boundary，而不是 socket-level
        streaming cap。adapter 会拒绝未知或超限的 ``get_file`` 大小，并在下载后再次
        核验实际长度和 SHA-256。它刻意不调用 ``File.download_as_bytearray``，因为 PTB
        会把本机存在的 ``file_path`` 视为 local file。/ PTB 22's
        ``BaseRequest.retrieve`` returns bytes after the client receives the full response, so
        the ``file_size`` preflight here is a provider-side boundary rather than a socket-level
        streaming cap. The adapter rejects unknown or over-limit ``get_file`` sizes and verifies
        the actual length and SHA-256 again after downloading. It deliberately does not call
        ``File.download_as_bytearray``, because PTB treats an existing ``file_path`` on the host
        as a local file.
    """

    def __init__(
        self,
        *,
        bot: TelegramAttachmentBot,
        http: TelegramHttpTimeoutSettings,
    ) -> None:
        """@brief 注入已初始化 Bot 与既有 HTTP 超时 / Inject the initialized Bot and existing HTTP timeouts.

        @param bot 由 ``Application`` 共享的已初始化 Telegram Bot / Initialized Telegram Bot shared by ``Application``.
        @param http ``settings.telegram.http`` 形状的已验证超时 / Validated timeouts shaped like ``settings.telegram.http``.
        @raise TypeError Bot 或超时字段类型非法时抛出 / Raised when the Bot or timeout fields have invalid types.
        @raise ValueError 任一超时不为有限正数时抛出 / Raised when any timeout is not finite and positive.
        """

        if bot is None:
            raise TypeError("Telegram current-turn upload source requires a Bot")
        self._bot = bot
        self._timeouts = _timeouts_from(http)

    async def download(
        self,
        reference: CurrentTurnUploadReference,
    ) -> DownloadedCurrentTurnUpload:
        """@brief 下载 durable 引用唯一授权的附件 / Download the sole attachment authorized by a durable reference.

        @param reference durable Assistant command 中的当前 Turn 引用；不接受调用方提供
            的 file ID / Current-Turn reference from a durable Assistant command; no caller-
            supplied file ID is accepted.
        @return 不可变、至多 8 MiB 且已摘要核验的结果 / Immutable result of at most 8 MiB with a verified digest.
        @raise CurrentTurnUploadTooLargeError 任一声明或实际大小超限时抛出 /
            Raised when any declared or actual size exceeds the limit.
        @raise CurrentTurnUploadIntegrityError ``get_file`` 返回了不同文件时抛出 /
            Raised when ``get_file`` returned a different file.
        @raise CurrentTurnUploadTransportError 已知 Telegram 网络或超时边界失败时抛出 /
            Raised for a known Telegram network or timeout boundary failure.
        @raise CurrentTurnUploadDownloadError provider 对象不满足内存下载契约时抛出 /
            Raised when a provider object violates the in-memory download contract.
        @note 此方法不创建、打开或写入本地文件。/ This method creates, opens, and writes no local files.
        @note 底层 request 失败会被无 URL 的错误替代，避免把含 Bot capability 的下载 URL
            写入日志。/ A lower-level request failure is replaced with a URL-free error so a
            download URL containing a Bot capability cannot enter logs.
        """

        if not isinstance(reference, CurrentTurnUploadReference):
            raise TypeError(
                "Telegram current-turn upload source requires a CurrentTurnUploadReference"
            )
        _validate_declared_size(reference.declared_byte_size)
        resolved: TelegramAttachmentFile | None = None
        metadata_transport_failed = False
        try:
            resolved = await self._bot.get_file(
                reference.file_id,
                read_timeout=self._timeouts.read,
                write_timeout=self._timeouts.write,
                connect_timeout=self._timeouts.connect,
                pool_timeout=self._timeouts.pool,
            )
        except (NetworkError, OSError, TimeoutError):
            # A provider exception can carry request details.  Map only known transport
            # failures and deliberately discard its text before durable error handling.
            # provider 异常可能含请求细节。这里只映射已知传输失败，并在进入 durable 错误处理前
            # 刻意丢弃其文本。
            metadata_transport_failed = True
        if metadata_transport_failed:
            # Deliberately raise *after* the ``except`` suite.  ``raise ... from None`` only
            # suppresses traceback rendering while retaining ``__context__`` for a structured
            # error collector to inspect; leaving the suite first removes the capability-bearing
            # provider exception from the object graph as well.  必须在 ``except`` 语句块外抛出：
            # ``raise ... from None`` 只隐藏 traceback，仍会在 ``__context__`` 留下 provider
            # 异常供结构化错误收集器读取；离开语句块后再抛出才会一并清除 capability 对象图。
            raise CurrentTurnUploadTransportError(
                "Telegram attachment metadata retrieval failed"
            )
        if resolved is None:
            raise CurrentTurnUploadDownloadError(
                "Telegram get_file did not return an attachment descriptor"
            )
        _validate_resolved_file(
            resolved,
            expected_file_id=reference.file_id,
            expected_file_unique_id=reference.file_unique_id,
        )
        download_url = _validated_remote_https_url(getattr(resolved, "file_path", None))
        payload: object = None
        retrieval_transport_failed = False
        try:
            payload = await self._bot.request.retrieve(
                download_url,
                read_timeout=self._timeouts.read,
                write_timeout=self._timeouts.write,
                connect_timeout=self._timeouts.connect,
                pool_timeout=self._timeouts.pool,
            )
        except (NetworkError, OSError, TimeoutError):
            # PTB ``file_path`` 的 HTTPS URL 含 Bot capability，不能让底层异常文本把它
            # 带入 activity 日志。 PTB's HTTPS ``file_path`` URL contains a Bot capability, so
            # no lower-level exception text may carry it into activity logs.
            retrieval_transport_failed = True
        if retrieval_transport_failed:
            # See the metadata branch above: raise outside the exception suite so the original
            # capability-bearing URL cannot survive in ``__context__``.
            raise CurrentTurnUploadTransportError(
                "Telegram attachment network retrieval failed"
            )
        if not isinstance(payload, bytes):
            raise CurrentTurnUploadDownloadError(
                "Telegram request did not return immutable bytes"
            )
        byte_size = len(payload)
        if byte_size > CURRENT_TURN_UPLOAD_MAX_BYTES:
            raise CurrentTurnUploadTooLargeError(
                "Telegram attachment exceeds the 8 MiB limit after download"
            )
        content = bytes(payload)
        digest = hashlib.sha256(content).hexdigest()
        return DownloadedCurrentTurnUpload(
            content=content,
            byte_size=byte_size,
            sha256=digest,
            original_file_name=reference.original_file_name,
            mime_type=reference.mime_type,
        )


def _timeouts_from(http: TelegramHttpTimeoutSettings) -> _TelegramDownloadTimeouts:
    """@brief 从已有 Telegram HTTP 设置复制并校验超时 / Copy and validate timeouts from existing Telegram HTTP settings.

    @param http ``settings.telegram.http`` 形状的超时对象 / Timeout object shaped like ``settings.telegram.http``.
    @return 不可变已校验超时 / Immutable validated timeouts.
    @raise TypeError 字段缺失或类型非法时抛出 / Raised when a field is missing or has an invalid type.
    @raise ValueError 超时不为有限正数时抛出 / Raised when a timeout is not finite and positive.
    """

    return _TelegramDownloadTimeouts(
        connect=_positive_timeout(http, "connect_timeout_seconds"),
        read=_positive_timeout(http, "read_timeout_seconds"),
        write=_positive_timeout(http, "write_timeout_seconds"),
        pool=_positive_timeout(http, "pool_timeout_seconds"),
    )


def _positive_timeout(http: TelegramHttpTimeoutSettings, name: str) -> float:
    """@brief 读取一个有限正 Telegram HTTP 超时 / Read one finite positive Telegram HTTP timeout.

    @param http 超时设置对象 / Timeout-settings object.
    @param name 属性名 / Attribute name.
    @return 可传给 PTB 的秒数 / Seconds suitable for PTB.
    @raise TypeError 属性不是数值时抛出 / Raised when the attribute is not numeric.
    @raise ValueError 属性不是有限正数时抛出 / Raised when the attribute is not finite and positive.
    """

    value = getattr(http, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Telegram HTTP {name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"Telegram HTTP {name} must be finite and positive")
    return normalized


def _validate_declared_size(size: int | None) -> None:
    """@brief 防御性复核 ingress 声明大小 / Defensively recheck the ingress-declared size.

    @param size ingress 声明的可选大小 / Optional size declared at ingress.
    @return None / None.
    @raise CurrentTurnUploadTooLargeError 声明大小超限时抛出 / Raised when the declared size exceeds the limit.
    """

    if size is not None and size > CURRENT_TURN_UPLOAD_MAX_BYTES:
        raise CurrentTurnUploadTooLargeError(
            "Telegram attachment exceeds the 8 MiB limit at ingress"
        )


def _validate_resolved_file(
    resolved: TelegramAttachmentFile,
    *,
    expected_file_id: str,
    expected_file_unique_id: str,
) -> None:
    """@brief 核验 ``get_file`` 身份、大小与远程 URL / Validate ``get_file`` identity, size, and remote URL.

    @param resolved ``get_file`` 返回的 File / File returned by ``get_file``.
    @param expected_file_id durable 引用已授权的 file ID / File ID authorized by the durable reference.
    @param expected_file_unique_id durable 引用已授权的稳定 file identity / Stable file identity authorized by the durable reference.
    @return None / None.
    @raise CurrentTurnUploadIntegrityError 返回身份与 durable 引用不一致时抛出 /
        Raised when the returned identity differs from the durable reference.
    @raise CurrentTurnUploadTooLargeError provider 声明大小超限时抛出 /
        Raised when the provider-declared size exceeds the limit.
    @raise CurrentTurnUploadDownloadError 大小未知或 URL 可触发 PTB local-file 路径时抛出 /
        Raised when the size is unknown or the URL could trigger PTB local-file handling.
    """

    file_id = getattr(resolved, "file_id", None)
    file_unique_id = getattr(resolved, "file_unique_id", None)
    if (
        not isinstance(file_id, str)
        or file_id != expected_file_id
        or not isinstance(file_unique_id, str)
        or file_unique_id != expected_file_unique_id
    ):
        raise CurrentTurnUploadIntegrityError(
            "Telegram get_file returned an unexpected attachment identity"
        )
    file_size = getattr(resolved, "file_size", None)
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        raise CurrentTurnUploadDownloadError(
            "Telegram get_file must provide a non-negative file_size"
        )
    if file_size > CURRENT_TURN_UPLOAD_MAX_BYTES:
        raise CurrentTurnUploadTooLargeError(
            "Telegram get_file reports an attachment above the 8 MiB limit"
        )
    _validated_remote_https_url(getattr(resolved, "file_path", None))


def _validated_remote_https_url(value: object) -> str:
    """@brief 验证并返回只允许网络读取的 HTTPS file URL / Validate and return an HTTPS file URL allowed only for network retrieval.

    @param value PTB ``File.file_path`` / PTB ``File.file_path``.
    @return 已验证的 HTTPS URL / Validated HTTPS URL.
    @raise CurrentTurnUploadDownloadError URL 缺失、非 HTTPS 或不是网络位置时抛出 /
        Raised when the URL is absent, non-HTTPS, or not a network location.
    @note adapter 随后经 ``Bot.request.retrieve`` 读取本 URL，而非调用 PTB File 的下载
        快捷方法；这不触发 PTB 对本机 ``file_path`` 的 local-file 分支。/ The adapter
        subsequently reads this URL through ``Bot.request.retrieve`` rather than a PTB File
        download shortcut; this does not trigger PTB's local-file branch for a host
        ``file_path``.
    """

    if not isinstance(value, str) or not value:
        raise CurrentTurnUploadDownloadError(
            "Telegram get_file did not provide a remote HTTPS download URL"
        )
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise CurrentTurnUploadDownloadError(
            "Telegram current-turn uploads require a remote HTTPS download URL"
        )
    return value


__all__ = [
    "TelegramCurrentTurnUploadSource",
    "TelegramAttachmentBot",
    "TelegramAttachmentFile",
    "TelegramFileRequest",
    "TelegramHttpTimeoutSettings",
]
