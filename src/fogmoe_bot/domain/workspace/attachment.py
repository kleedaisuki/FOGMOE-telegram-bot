"""@brief Workspace 附件导入的可见性语义 / Workspace-attachment import visibility semantics.

一个当前 Turn 附件在 native ``add_file`` 成功并被 durable receipt 见证前，绝不能成为
模型历史的一部分。此模块把该事实建模为会话消息 envelope 中一个受控的小状态机，而不是把
“看起来像路径”的文本当作文件存在证明。/ A current-Turn attachment must never become
part of model history before native ``add_file`` succeeds and a durable receipt witnesses it.
This module models that fact as a small controlled state machine in a conversation-message
envelope rather than treating path-looking text as proof that a file exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue

from .runtime import WorkspaceRequestHash, WorkspaceRequestId
from .scope import GroupRuntimeScope, PersonalRuntimeScope, RuntimeScope

WORKSPACE_ATTACHMENT_FIELD = "workspace_attachment"
"""@brief 会话 envelope 中附件导入状态字段 / Attachment-import state field in a conversation envelope."""

WORKSPACE_ATTACHMENT_MARKER_VERSION = 1
"""@brief 附件可见性 marker 的 wire 版本 / Wire version of the attachment-visibility marker."""

MAX_WORKSPACE_ATTACHMENT_BYTES = 8 * 1024 * 1024
"""@brief 一个当前附件可持久导入的最大字节数 / Maximum bytes persistable for one current attachment."""

_ATTACHMENT_OPAQUE_ID_PATTERN = re.compile(r"^attachment-[0-9a-f]{64}$")
"""@brief 当前附件受信任 opaque directory ID 的语法 / Grammar of a trusted current-attachment opaque directory ID."""

_ATTACHMENT_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""@brief 当前附件内容 SHA-256 的规范语法 / Canonical grammar of a current-attachment content SHA-256."""


@dataclass(frozen=True, slots=True)
class AttachmentImportIntent:
    """@brief native 副作用之前持久化的附件导入意图聚合 / Attachment-import intent aggregate persisted before the native side effect.

    @param turn_id 产生该附件的 durable Turn / Durable Turn that owns the attachment.
    @param conversation_id 拥有该 Turn 的会话 / Conversation owning the Turn.
    @param source_message_id 当前附件占位符所在的唯一 user 消息 / Sole user message carrying the attachment placeholder.
    @param scope 文件应进入的个人或整群 Workspace / Personal or whole-group Workspace receiving the file.
    @param opaque_id 固定 uploads 目录的可信 ID / Trusted ID of the fixed uploads directory.
    @param request_id native payload journal 的稳定调用 ID / Stable invocation ID of the native payload journal.
    @param request_hash 完整导入语义的不可变摘要 / Immutable digest of complete import semantics.
    @param byte_size 已验证 payload 的字节数 / Byte count of the verified payload.
    @param sha256 已验证 payload 的内容摘要 / Content digest of the verified payload.
    @note 这是 ``AttachmentImportIntent`` 聚合根（aggregate root），而不是一次下载的临时
        DTO。它把 durable Conversation source 与尚未发生的 native ``add_file`` 副作用桥接
        起来：一经准备成功，重试必须首先查询相同 journal，而不得依赖可能失效的 provider
        capability。/ This is an ``AttachmentImportIntent`` aggregate root, not a transient
        download DTO. It bridges the durable Conversation source to the not-yet-performed native
        ``add_file`` side effect: once prepared, a retry must query the same journal first rather
        than depend on an expiring provider capability.
    """

    turn_id: TurnId
    """@brief 当前 durable Turn / Current durable Turn."""
    conversation_id: ConversationId
    """@brief 所属会话 / Owning conversation."""
    source_message_id: ConversationMessageId
    """@brief 当前附件 source user message / Current attachment source user message."""
    scope: RuntimeScope
    """@brief 目标 Workspace 的强类型归属 / Typed ownership of the destination Workspace."""
    opaque_id: str
    """@brief 固定 uploads 目录 ID / Fixed uploads-directory ID."""
    request_id: WorkspaceRequestId
    """@brief native journal request ID / Native journal request ID."""
    request_hash: WorkspaceRequestHash
    """@brief native journal request hash / Native journal request hash."""
    byte_size: int
    """@brief 已验证 payload 字节数 / Verified payload byte count."""
    sha256: str
    """@brief 已验证 payload SHA-256 / Verified payload SHA-256."""

    def __post_init__(self) -> None:
        """@brief 校验聚合的不可变业务语义 / Validate immutable aggregate business semantics.

        @return None / None.
        @raise TypeError identity、scope 或标量类型不正确时抛出 / Raised when identities, scope, or scalar types are invalid.
        @raise ValueError request、路径身份、大小或摘要不满足当前附件协议时抛出 / Raised when request, path identity, size, or digest violates the current-attachment protocol.
        """

        if not isinstance(self.turn_id, TurnId):
            raise TypeError("Attachment import intent requires a TurnId")
        if not isinstance(self.conversation_id, ConversationId):
            raise TypeError("Attachment import intent requires a ConversationId")
        if not isinstance(self.source_message_id, ConversationMessageId):
            raise TypeError("Attachment import intent requires a ConversationMessageId")
        if not isinstance(self.scope, PersonalRuntimeScope | GroupRuntimeScope):
            raise TypeError("Attachment import intent requires a typed workspace scope")
        if not isinstance(self.opaque_id, str):
            raise TypeError("Attachment import intent opaque_id must be a string")
        if _ATTACHMENT_OPAQUE_ID_PATTERN.fullmatch(self.opaque_id) is None:
            raise ValueError(
                "Attachment import intent opaque_id is not a fixed attachment ID"
            )
        if not isinstance(self.request_id, WorkspaceRequestId):
            raise TypeError("Attachment import intent requires a WorkspaceRequestId")
        if self.request_id.value != f"{self.turn_id}:attachment-import":
            raise ValueError(
                "Attachment import intent request_id does not belong to its Turn"
            )
        if not isinstance(self.request_hash, WorkspaceRequestHash):
            raise TypeError("Attachment import intent requires a WorkspaceRequestHash")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("Attachment import intent byte_size must be an integer")
        if not 0 <= self.byte_size <= MAX_WORKSPACE_ATTACHMENT_BYTES:
            raise ValueError(
                "Attachment import intent byte_size exceeds the attachment budget"
            )
        if not isinstance(self.sha256, str):
            raise TypeError("Attachment import intent sha256 must be a string")
        if _ATTACHMENT_SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError(
                "Attachment import intent sha256 must be lowercase SHA-256"
            )

    @property
    def path(self) -> str:
        """@brief 返回 intent 唯一允许的 runtime payload 路径 / Return the sole runtime payload path allowed by the intent.

        @return Workspace 内固定 uploads payload 路径 / Fixed uploads payload path inside the Workspace.
        """

        return f"/workspace/uploads/{self.opaque_id}/payload"


class WorkspaceAttachmentImportState(StrEnum):
    """@brief 当前附件的 durable 导入可见性 / Durable import visibility of a current attachment."""

    PENDING = "pending"
    """@brief 尚无可验证 receipt，任何模型投影都必须隐藏 / No verifiable receipt yet; every model projection must hide it."""

    IMPORTED = "imported"
    """@brief receipt 已原子持久化，路径可作为历史数据呈现 / Receipt was atomically persisted; the path may appear as history data."""

    UNAVAILABLE = "unavailable"
    """@brief 旧行或终态失败没有 receipt，永远不得伪造路径 / Legacy or terminally failed row has no receipt and must never fabricate a path."""


def pending_workspace_attachment_marker() -> JsonObject:
    """@brief 创建新 ingress 附件的 pending marker / Create the pending marker for a newly ingressed attachment.

    @return 只含固定版本和 pending 状态的新 JSON object / Fresh JSON object containing only the fixed version and pending state.
    @note 调用方必须把这个 marker 与 ``current_turn_upload`` 同一事务持久化；它不包含
        provider capability、文件名、MIME、bytes 或路径。/ Callers must persist this marker
        atomically with ``current_turn_upload``; it contains no provider capability, filename,
        MIME type, bytes, or path.
    """

    return {
        "version": WORKSPACE_ATTACHMENT_MARKER_VERSION,
        "state": WorkspaceAttachmentImportState.PENDING.value,
    }


def workspace_attachment_import_state(
    content: Mapping[str, JsonValue],
) -> WorkspaceAttachmentImportState | None:
    """@brief 读取严格有效的附件 marker 状态 / Read the strictly valid attachment-marker state.

    @param content durable conversation-message envelope / Durable conversation-message envelope.
    @return 合法状态；无 marker 或 marker 畸形时为 ``None`` / Valid state, or ``None`` when absent or malformed.
    @note ``None`` 不代表模型一定可见：请使用
        ``workspace_attachment_is_model_visible`` 执行 fail-closed 可见性判断。/ ``None``
        does not necessarily mean model-visible: use
        ``workspace_attachment_is_model_visible`` for the fail-closed visibility decision.
    """

    marker = content.get(WORKSPACE_ATTACHMENT_FIELD)
    if not isinstance(marker, Mapping):
        return None
    version = marker.get("version")
    state = marker.get("state")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != WORKSPACE_ATTACHMENT_MARKER_VERSION
        or not isinstance(state, str)
    ):
        return None
    try:
        return WorkspaceAttachmentImportState(state)
    except ValueError:
        return None


def workspace_attachment_is_model_visible(content: Mapping[str, JsonValue]) -> bool:
    """@brief 判断一条消息可否进入普通模型派生面 / Determine whether a message may enter ordinary model-derived surfaces.

    @param content durable conversation-message envelope / Durable conversation-message envelope.
    @return 无附件 marker 时为 True；有 marker 时仅 receipt 见证的 imported 为 True /
        ``True`` without an attachment marker; with a marker, ``True`` only for
        receipt-witnessed imported state.
    @note marker 存在但未知版本、畸形结构或非 imported 状态一律 fail-closed。/ A present
        marker with an unknown version, malformed structure, or a non-imported state always
        fails closed.
    """

    if WORKSPACE_ATTACHMENT_FIELD not in content:
        return True
    return (
        workspace_attachment_import_state(content)
        is WorkspaceAttachmentImportState.IMPORTED
    )


def workspace_attachment_blocks_compaction(content: Mapping[str, JsonValue]) -> bool:
    """@brief 判断未决附件是否必须阻止 compaction 越过它 / Determine whether an unresolved attachment must block compaction from crossing it.

    @param content durable conversation-message envelope / Durable conversation-message envelope.
    @return pending 或畸形 marker 时为 True；无 marker、imported、unavailable 时为 False /
        ``True`` for pending or malformed markers; ``False`` for no marker, imported, or unavailable.
    @note 不能把 pending 行压缩为空摘要再在日后发布 receipt，否则成功附件会永久丢失其
        历史可见性。/ A pending row must not be compacted into an empty summary and then later
        receive a receipt, otherwise a successfully imported attachment would permanently lose
        its historical visibility.
    """

    if WORKSPACE_ATTACHMENT_FIELD not in content:
        return False
    state = workspace_attachment_import_state(content)
    return state not in {
        WorkspaceAttachmentImportState.IMPORTED,
        WorkspaceAttachmentImportState.UNAVAILABLE,
    }


__all__ = [
    "AttachmentImportIntent",
    "MAX_WORKSPACE_ATTACHMENT_BYTES",
    "WORKSPACE_ATTACHMENT_FIELD",
    "WORKSPACE_ATTACHMENT_MARKER_VERSION",
    "WorkspaceAttachmentImportState",
    "pending_workspace_attachment_marker",
    "workspace_attachment_blocks_compaction",
    "workspace_attachment_import_state",
    "workspace_attachment_is_model_visible",
]
