"""@brief 当前附件 native 发布的 durable receipt 契约 / Durable receipt contract for current-attachment native publication.

该应用层契约把 native ``RuntimeProcess.add_file`` 的成功结果与 Conversation 的可见性状态
连接起来。它不暴露给 Agent，也不持有 Telegram capability 或内容 bytes。/ This application
contract connects a successful native ``RuntimeProcess.add_file`` result to Conversation
visibility state. It is not exposed to the Agent and holds neither Telegram capabilities nor
content bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.application.workspace.models import AddFileResult
from fogmoe_bot.domain.conversation.identity import ConversationId, TurnId
from fogmoe_bot.domain.workspace.runtime import (
    WorkspaceRequestHash,
    WorkspaceRequestId,
)
from fogmoe_bot.domain.workspace.scope import (
    GroupRuntimeScope,
    PersonalRuntimeScope,
    RuntimeScope,
)

from .inference_command import DurableAssistantInferenceCommand


class WorkspaceAttachmentReceiptError(RuntimeError):
    """@brief 附件 receipt 持久化失败的基类 / Base error for attachment-receipt persistence failure."""


class WorkspaceAttachmentReceiptConflictError(WorkspaceAttachmentReceiptError):
    """@brief receipt 或 pending 消息与不可变语义冲突 / Receipt or pending message conflicts with immutable semantics."""


class WorkspaceAttachmentReceiptUnavailableError(WorkspaceAttachmentReceiptError):
    """@brief receipt 存储暂时不可用 / Receipt storage is temporarily unavailable."""


class ConversationHistoryInvalidator(Protocol):
    """@brief receipt 发布后失效会话历史缓存的窄端口 / Narrow port invalidating conversation-history caches after receipt publication."""

    def invalidate(self, conversation_id: ConversationId) -> None:
        """@brief 使一个会话的本地历史投影失效 / Invalidate local history projections for one conversation.

        @param conversation_id receipt 刚发布的会话 / Conversation whose receipt was just published.
        @return None / None.
        @note 这是 application 事件后的本地投影维护，不是数据库 adapter 的职责。跨进程
            正确性还由 ContextWindow 对 pending 行不缓存保证。/ This is local projection
            maintenance after an application event, not a database-adapter responsibility.
            Cross-process correctness is additionally protected by ContextWindow refusing to
            cache pending rows.
        """

        ...


@dataclass(frozen=True, slots=True)
class WorkspaceAttachmentImportReceipt:
    """@brief 已由 native ``add_file`` 发布、待 durable 见证的附件事实 / Attachment fact published by native ``add_file`` and awaiting durable witnessing.

    @param turn_id 当前 durable Turn / Current durable Turn.
    @param conversation_id 所属长期会话 / Owning long-lived conversation.
    @param scope 文件所属个人或群 Workspace / Personal or group Workspace owning the file.
    @param request_id native journal 的稳定请求 ID / Stable request ID in the native journal.
    @param request_hash 完整导入意图摘要 / Complete import-intent digest.
    @param path runtime 内已发布 payload 路径 / Published payload path inside the runtime.
    @param byte_size 已核验字节数 / Verified byte count.
    @param sha256 已核验内容摘要 / Verified content digest.
    @note 此对象是 application 语义事实，而非模型输入；它没有文件名、MIME、provider
        file ID 或 bytes。/ This object is an application semantic fact, not model input; it has
        no filename, MIME, provider file ID, or bytes.
    """

    turn_id: TurnId
    conversation_id: ConversationId
    scope: RuntimeScope
    request_id: WorkspaceRequestId
    request_hash: WorkspaceRequestHash
    path: str
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        """@brief 复用 native receipt 的固定协议校验 / Reuse the fixed protocol validation of a native receipt.

        @return None / None.
        @raise TypeError identity、scope 或 receipt 字段类型非法时抛出 /
            Raised when identity, scope, or receipt field types are invalid.
        @raise ValueError 路径、大小或摘要不符合 native 协议时抛出 /
            Raised when path, size, or digest violates the native protocol.
        """

        if not isinstance(self.turn_id, TurnId):
            raise TypeError("Attachment receipt requires a TurnId")
        if not isinstance(self.conversation_id, ConversationId):
            raise TypeError("Attachment receipt requires a ConversationId")
        if not isinstance(self.scope, PersonalRuntimeScope | GroupRuntimeScope):
            raise TypeError("Attachment receipt requires a typed workspace scope")
        AddFileResult(
            request_id=self.request_id,
            replayed=False,
            path=self.path,
            byte_size=self.byte_size,
            sha256=self.sha256,
        )
        if not isinstance(self.request_hash, WorkspaceRequestHash):
            raise TypeError("Attachment receipt requires a WorkspaceRequestHash")


class WorkspaceAttachmentReceiptStore(Protocol):
    """@brief 将 native 附件事实原子见证到 Conversation 的端口 / Port atomically witnessing a native attachment fact into Conversation."""

    async def record_import(
        self,
        command: DurableAssistantInferenceCommand,
        receipt: WorkspaceAttachmentImportReceipt,
    ) -> None:
        """@brief 原子写入 immutable receipt 并将 pending 行发布为 imported / Atomically write an immutable receipt and publish the pending row as imported.

        @param command 已恢复且严格校验的 durable Assistant command / Restored and strictly validated durable Assistant command.
        @param receipt 刚由 native ``add_file`` 返回并经 application 核验的事实 /
            Fact just returned by native ``add_file`` and validated by the application.
        @return None / None.
        @raise WorkspaceAttachmentReceiptConflictError durable command、消息、receipt 或已有 replay
            语义不一致时抛出 / Raised when durable command, message, receipt, or an existing
            replay disagrees semantically.
        @raise WorkspaceAttachmentReceiptUnavailableError 存储暂时不可用时抛出 /
            Raised when storage is temporarily unavailable.
        @note 该方法是 native 成功与模型可见性之间的唯一 publish point；返回前不得让
            ``pending`` 路径进入模型。/ This method is the sole publish point between native
            success and model visibility; it must not let a pending path enter a model before it
            returns.
        """

        ...


__all__ = [
    "WorkspaceAttachmentImportReceipt",
    "ConversationHistoryInvalidator",
    "WorkspaceAttachmentReceiptConflictError",
    "WorkspaceAttachmentReceiptError",
    "WorkspaceAttachmentReceiptStore",
    "WorkspaceAttachmentReceiptUnavailableError",
]
