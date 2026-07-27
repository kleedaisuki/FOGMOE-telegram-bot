"""@brief 当前附件 native 副作用的 durable 意图契约 / Durable intent contract for a current attachment's native side effect.

``AttachmentImportIntent`` 不是 receipt 的前置状态字段，也不是可随意清理的重试表。它是
Conversation 限界上下文（bounded context）与 native payload journal 之间的明确桥：先提交
不可变意图，才允许调用 ``RuntimeProcess.add_file``；之后的重试先查询相同 journal，只有明确
``not_found`` 才能重新使用 provider capability。/ ``AttachmentImportIntent`` is neither a
pre-receipt status field nor a disposable retry table. It is the explicit bridge between the
Conversation bounded context and the native payload journal: its immutable intent commits before
``RuntimeProcess.add_file`` may run; a later retry queries that same journal first and may reuse a
provider capability only after an explicit ``not_found``.
"""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.domain.workspace.attachment import AttachmentImportIntent

from .inference_command import DurableAssistantInferenceCommand


class WorkspaceAttachmentIntentError(RuntimeError):
    """@brief 附件导入意图持久化失败的基类 / Base error for attachment-import intent persistence failure."""


class WorkspaceAttachmentIntentConflictError(WorkspaceAttachmentIntentError):
    """@brief 意图与 durable source、既有意图或状态机冲突 / Intent conflicts with its durable source, existing intent, or state machine."""


class WorkspaceAttachmentIntentUnavailableError(WorkspaceAttachmentIntentError):
    """@brief 意图存储暂时不可用 / Intent store is temporarily unavailable."""


class WorkspaceAttachmentImportIntentStore(Protocol):
    """@brief 读取及准备 ``AttachmentImportIntent`` 聚合的端口 / Port reading and preparing ``AttachmentImportIntent`` aggregates."""

    async def find(
        self,
        command: DurableAssistantInferenceCommand,
    ) -> AttachmentImportIntent | None:
        """@brief 按当前 durable Turn 查找已经准备的意图 / Find an already prepared intent by the current durable Turn.

        @param command 已恢复并严格校验的 durable Assistant command / Restored and strictly validated durable Assistant command.
        @return 已准备的不可变意图；尚未准备时为 None / Prepared immutable intent, or None when not prepared yet.
        @raise WorkspaceAttachmentIntentConflictError command 与持久意图不属于同一 source 或 scope 时抛出 /
            Raised when the command and persisted intent do not belong to the same source or scope.
        @raise WorkspaceAttachmentIntentUnavailableError 存储暂时不可用时抛出 /
            Raised when storage is temporarily unavailable.
        @note ``find`` 不下载 Telegram bytes，也不触发 native activation。/ ``find`` neither
            downloads Telegram bytes nor activates native runtime.
        """

        ...

    async def prepare(
        self,
        command: DurableAssistantInferenceCommand,
        intent: AttachmentImportIntent,
    ) -> AttachmentImportIntent:
        """@brief 原子持久化一次 native 调用之前的不可变意图 / Atomically persist one immutable intent before its native invocation.

        @param command 已恢复并严格校验的 durable Assistant command / Restored and strictly validated durable Assistant command.
        @param intent 已下载且校验 bytes 后构造的候选 aggregate / Candidate aggregate constructed after downloading and validating bytes.
        @return 当前 Turn 的唯一持久意图；并发者已创建等价意图时返回该意图 /
            The sole persisted intent for this Turn; returns it when a concurrent worker already prepared an equivalent one.
        @raise WorkspaceAttachmentIntentConflictError 候选与当前 source 或既有意图不兼容时抛出 /
            Raised when the candidate is incompatible with the current source or existing intent.
        @raise WorkspaceAttachmentIntentUnavailableError 存储暂时不可用时抛出 /
            Raised when storage is temporarily unavailable.
        @note 返回前 intent 必须已提交；调用方在此方法返回前不得调用 native ``add_file``。
            The intent must be committed before return; the caller must not invoke native
            ``add_file`` before this method returns.
        """

        ...


__all__ = [
    "WorkspaceAttachmentImportIntentStore",
    "WorkspaceAttachmentIntentConflictError",
    "WorkspaceAttachmentIntentError",
    "WorkspaceAttachmentIntentUnavailableError",
]
