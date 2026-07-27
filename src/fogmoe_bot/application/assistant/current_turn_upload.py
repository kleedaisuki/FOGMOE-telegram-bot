"""@brief 当前 Turn 上传引用与受限下载端口 / Current-Turn upload references and bounded-download port.

该模块定义 durable ingress 与 Agent 前 application importer 共享的、与 Telegram SDK
无关的强类型契约。引用只是当前 Telegram 附件的授权身份和展示元数据；下载结果
才会包含内存中的 bytes，且永远不携带宿主机路径或工作区路径。/ This module defines the
strongly typed, Telegram-SDK-independent contract shared between durable ingress and the
application importer before the Agent. A reference contains only the authorized identity and
display metadata of the current Telegram attachment; only a download result contains in-memory
bytes, and it never carries a host or workspace path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from fogmoe_bot.domain.conversation.identity import TurnId


CURRENT_TURN_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
"""@brief 当前 Turn 上传的最大实际字节数 / Maximum actual byte size of a current-Turn upload."""

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
"""@brief 小写 SHA-256 十六进制格式 / Lowercase SHA-256 hexadecimal grammar."""

_WORKSPACE_ATTACHMENT_ID_PREFIX = "attachment-"
"""@brief Workspace 当前附件目录的可信前缀 / Trusted prefix for current-attachment Workspace directories."""


class CurrentTurnUploadKind(StrEnum):
    """@brief 当前 Turn 可导入的 Telegram 附件类别 / Telegram attachment kinds importable for the current Turn.

    @note 这是 Telegram presentation 层媒体类别在 application 边界的紧凑投影；它只约束
        durable source 的身份语义，不决定解析、MIME、文件名或 Workspace 内的执行权限。
        This is a compact application-boundary projection of Telegram presentation media kinds;
        it constrains only durable-source identity semantics and does not determine parsing, MIME,
        filenames, or execution permissions inside the Workspace.
    """

    PHOTO = "photo"
    """@brief Telegram 图片附件 / Telegram photo attachment."""

    STICKER = "sticker"
    """@brief Telegram 贴纸附件 / Telegram sticker attachment."""

    DOCUMENT = "document"
    """@brief Telegram 文档附件 / Telegram document attachment."""


class CurrentTurnUploadReference(BaseModel):
    """@brief 当前 Turn 唯一授权的 Telegram 附件引用 / Sole Telegram attachment reference authorized for the current Turn.

    @param kind 附件的受限 presentation 类别 / Constrained presentation kind of the attachment.
    @param file_id Telegram 下载能力标识；只能由已接受的 Update 提供 /
        Telegram download capability identifier, supplied only by the accepted Update.
    @param file_unique_id 稳定内容身份；用于 receipt request-hash 而非下载 /
        Stable content identity used for the receipt request hash rather than downloading.
    @param source_update_id 产生此引用的 Telegram Update ID / Telegram Update ID that produced this reference.
    @param source_message_id 产生此引用的 Telegram message ID / Telegram message ID that produced this reference.
    @param declared_byte_size ingress 时 Telegram 声明的可选大小 / Optional size declared by Telegram at ingress.
    @param original_file_name 仅展示用的原始文件名；绝不能解释为路径 /
        Display-only original filename; it must never be interpreted as a path.
    @param mime_type Telegram 报告的可选 MIME 类型；仅为元数据 /
        Optional MIME type reported by Telegram; metadata only.

    @note ``file_id`` 是 provider capability，只能由 application importer 在 Agent 前
        使用；它不会成为模型工具参数。``file_unique_id``、Update ID 与 message ID 把
        引用绑定到已接受的 durable source attachment。超限的声明大小可暂存在此引用中，以
        便 ingress 先产生稳定的用户反馈；它绝不会进入可执行的 durable command 或下载
        步骤。/ ``file_id`` is a provider capability that may be used only by the application
        importer before the Agent; it never becomes a model-tool argument. ``file_unique_id``,
        Update ID, and message ID bind the reference to the accepted durable source attachment. An
        oversized declared size may temporarily exist in this reference so ingress can produce
        stable user feedback; it never reaches an executable durable command or a download step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    """@brief 禁止未知字段、隐式强制转换与就地修改 / Forbid unknown fields, coercion, and in-place mutation."""

    kind: CurrentTurnUploadKind = CurrentTurnUploadKind.DOCUMENT
    """@brief 附件的应用层种类；缺省 Document 以读取早期 durable 行 / Application kind; defaults to Document when reading early durable rows."""
    file_id: str = Field(min_length=1, max_length=1024)
    """@brief Telegram 下载能力 ID / Telegram download capability ID."""
    file_unique_id: str = Field(min_length=1, max_length=1024)
    """@brief Telegram 稳定文件 identity / Telegram stable file identity."""
    source_update_id: int = Field(ge=0)
    """@brief 来源 Update ID / Source Update ID."""
    source_message_id: int = Field(ge=1)
    """@brief 来源消息 ID / Source message ID."""
    declared_byte_size: int | None = Field(
        default=None,
        ge=1,
    )
    """@brief ingress 时已知的声明字节数 / Byte size declared at ingress when known."""
    original_file_name: str | None = Field(default=None, max_length=1024)
    """@brief 非路径语义的原始文件名 / Original filename without path semantics."""
    mime_type: str | None = Field(default=None, max_length=255)
    """@brief Telegram 报告的 MIME 类型 / MIME type reported by Telegram."""


class CurrentTurnUploadError(RuntimeError):
    """@brief 当前 Turn 附件不能安全提供 / Current-Turn attachment cannot be provided safely."""


class CurrentTurnUploadUnavailableError(CurrentTurnUploadError):
    """@brief 当前 durable command 没有已授权附件 / Current durable command has no authorized attachment."""


class CurrentTurnUploadTooLargeError(CurrentTurnUploadError):
    """@brief 附件在任一大小核验点超过上限 / Attachment exceeded the limit at a size-validation point."""


class CurrentTurnUploadIntegrityError(CurrentTurnUploadError):
    """@brief provider 返回的文件身份或内容摘要不匹配 / Provider returned a mismatched file identity or content digest."""


class CurrentTurnUploadTransportError(CurrentTurnUploadError):
    """@brief Telegram provider 的可重试传输失败 / Retryable transport failure from the Telegram provider.

    @note 该类型只表示已知网络/超时边界失败；provider 返回的畸形对象、非 bytes 响应和
        身份漂移仍是 ``CurrentTurnUploadDownloadError`` 或
        ``CurrentTurnUploadIntegrityError``，不能被误判为可无限重试。/ This type means
        only a known network/timeout boundary failure. A malformed provider object, non-bytes
        response, or identity drift remains a ``CurrentTurnUploadDownloadError`` or
        ``CurrentTurnUploadIntegrityError`` and must not be misclassified as indefinitely
        retryable.
    """


class CurrentTurnUploadDownloadError(CurrentTurnUploadError):
    """@brief provider 返回的下载对象不满足内存契约 / Provider returned a download object violating the in-memory contract."""


@dataclass(frozen=True, slots=True)
class DownloadedCurrentTurnUpload:
    """@brief 已下载且经核验的当前 Turn 附件 / Downloaded and verified current-Turn attachment.

    @param content 不可变、至多 8 MiB 的内存 bytes / Immutable in-memory bytes of at most 8 MiB.
    @param byte_size ``content`` 的实际字节数 / Actual byte size of ``content``.
    @param sha256 ``content`` 的小写 SHA-256 / Lowercase SHA-256 of ``content``.
    @param original_file_name 不带路径语义的原始文件名 / Original filename without path semantics.
    @param mime_type 仅供展示或审计的 MIME 元数据 / MIME metadata for display or audit only.

    @note 结果不决定工作区目标路径，也不解析或执行内容。/ The result does not choose a
        workspace destination path and does not parse or execute content.
    """

    content: bytes
    byte_size: int
    sha256: str
    original_file_name: str | None
    mime_type: str | None

    def __post_init__(self) -> None:
        """@brief 校验 bytes、大小与摘要的一致性 / Validate consistency of bytes, size, and digest.

        @return None / None.
        @raise TypeError 字段类型不符合严格结果契约时抛出 /
            Raised when a field violates the strict result contract.
        @raise ValueError bytes、大小或摘要不一致时抛出 /
            Raised when bytes, size, or digest are inconsistent.
        """

        if not isinstance(self.content, bytes):
            raise TypeError("Current-turn upload content must be immutable bytes")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("Current-turn upload byte_size must be an integer")
        if self.byte_size != len(self.content):
            raise ValueError("Current-turn upload byte_size does not match content")
        if self.byte_size > CURRENT_TURN_UPLOAD_MAX_BYTES:
            raise CurrentTurnUploadTooLargeError(
                "Current-turn upload exceeds the 8 MiB limit"
            )
        if not isinstance(self.sha256, str) or _SHA256_HEX.fullmatch(self.sha256) is None:
            raise ValueError("Current-turn upload sha256 must be lowercase SHA-256 hex")
        actual_digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 != actual_digest:
            raise CurrentTurnUploadIntegrityError(
                "Current-turn upload sha256 does not match content"
            )
        if self.original_file_name is not None and not isinstance(
            self.original_file_name, str
        ):
            raise TypeError("Current-turn upload original_file_name must be a string or None")
        if self.mime_type is not None and not isinstance(self.mime_type, str):
            raise TypeError("Current-turn upload mime_type must be a string or None")


class CurrentTurnUploadSource(Protocol):
    """@brief 只下载 durable 当前 Turn 附件引用的窄端口 / Narrow port downloading only a durable current-Turn attachment reference."""

    async def download(
        self,
        reference: CurrentTurnUploadReference,
    ) -> DownloadedCurrentTurnUpload:
        """@brief 下载已接受引用的当前 Turn 附件 / Download the current-Turn attachment of an accepted reference.

        @param reference 从 durable Assistant command 取得的强类型引用；调用方不能另传
            ``file_id`` / Strongly typed reference obtained from the durable Assistant command;
            callers cannot supply another ``file_id``.
        @return 已核验的内存 payload / Verified in-memory payload.
        @raise CurrentTurnUploadError provider 结果不满足边界时抛出 /
            Raised when a provider result violates a boundary.
        """

        ...


def workspace_attachment_opaque_id(
    *,
    turn_id: TurnId,
    reference: CurrentTurnUploadReference,
) -> str:
    """@brief 派生当前附件唯一的 Workspace 目录 ID / Derive the unique Workspace directory ID for a current attachment.

    @param turn_id 已接受来源确定的稳定 Turn ID / Stable Turn ID determined by the accepted source.
    @param reference 当前 Turn 已授权附件引用 / Authorized current-Turn attachment reference.
    @return 满足 native opaque-ID 语法的稳定目录标识 / Stable directory identifier satisfying native opaque-ID grammar.
    @raise TypeError 输入不是强类型 Turn/reference 时抛出 / Raised when inputs are not the typed Turn/reference.
    @note 此函数是 pure application policy，供 durable ingress 写入的模型占位符与
        Agent 前 importer 共用。它绝不使用 ``file_id``、文件名或 MIME，因此 provider
        capability 和用户声明不能影响路径。/ This is pure application policy shared by the
        model placeholder persisted at durable ingress and the pre-Agent importer. It never uses
        ``file_id``, filename, or MIME, so a provider capability or user declaration cannot affect
        the path.
    """

    if not isinstance(turn_id, TurnId):
        raise TypeError("Workspace attachment identity requires a TurnId")
    if not isinstance(reference, CurrentTurnUploadReference):
        raise TypeError(
            "Workspace attachment identity requires a CurrentTurnUploadReference"
        )
    payload = {
        "file_unique_id": reference.file_unique_id,
        "source_message_id": reference.source_message_id,
        "source_update_id": reference.source_update_id,
        "turn_id": str(turn_id),
        "version": 1,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_WORKSPACE_ATTACHMENT_ID_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


def workspace_attachment_file_path(
    *,
    turn_id: TurnId,
    reference: CurrentTurnUploadReference,
) -> str:
    """@brief 返回当前附件唯一可见的 runtime 文件路径 / Return the sole runtime file path visible for a current attachment.

    @param turn_id 已接受来源确定的稳定 Turn ID / Stable Turn ID determined by the accepted source.
    @param reference 当前 Turn 已授权附件引用 / Authorized current-Turn attachment reference.
    @return ``/workspace/uploads/<opaque-id>/payload`` / ``/workspace/uploads/<opaque-id>/payload``.
    @note 这是 runtime 内路径而非 host path；它在 native ``add_file`` 成功前仅是将要发布
        的逻辑目标。/ This is a runtime-internal path rather than a host path; before native
        ``add_file`` succeeds it is only the logical target to be published.
    """

    opaque_id = workspace_attachment_opaque_id(
        turn_id=turn_id,
        reference=reference,
    )
    return f"/workspace/uploads/{opaque_id}/payload"


__all__ = [
    "CURRENT_TURN_UPLOAD_MAX_BYTES",
    "CurrentTurnUploadDownloadError",
    "CurrentTurnUploadError",
    "CurrentTurnUploadIntegrityError",
    "CurrentTurnUploadKind",
    "CurrentTurnUploadReference",
    "CurrentTurnUploadSource",
    "CurrentTurnUploadTooLargeError",
    "CurrentTurnUploadTransportError",
    "CurrentTurnUploadUnavailableError",
    "DownloadedCurrentTurnUpload",
    "workspace_attachment_file_path",
    "workspace_attachment_opaque_id",
]
