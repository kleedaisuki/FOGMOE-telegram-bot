"""@brief 当前 Turn 附件到 Workspace 的预处理用例 / Current-Turn attachment-to-Workspace preprocessing use case.

该用例位于 Agent 调用之前：它只将已接受的 Telegram 附件通过 ``RuntimeProcess``
导入所属 Workspace，并把模型可见内容收束为固定路径占位符。它不是 Agent tool，模型不
接触 Telegram ``file_id``、原始文件名、MIME、字节或宿主机路径。/ This use case runs before
the Agent call: it imports an accepted Telegram attachment into its owning Workspace solely through
``RuntimeProcess`` and reduces model-visible content to a fixed-path placeholder. It is not an
Agent tool; the model receives no Telegram ``file_id``, original filename, MIME, bytes, or host
path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass

from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeProtocolError
from fogmoe_bot.application.workspace.models import (
    MAX_ADD_FILE_CHUNK_BYTES,
    AddFileCommand,
    AddFileResult,
)
from fogmoe_bot.application.workspace.ports import RuntimeProcess
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.conversation.identity import TurnId
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.workspace.runtime import WorkspaceRequestHash, WorkspaceRequestId
from fogmoe_bot.domain.workspace.scope import (
    GroupRuntimeScope,
    PersonalRuntimeScope,
    RuntimeScope,
)

from .current_turn_upload import (
    CurrentTurnUploadReference,
    CurrentTurnUploadSource,
    DownloadedCurrentTurnUpload,
    workspace_attachment_opaque_id,
)
from .inference_command import DurableAssistantInferenceCommand

_ATTACHMENT_REQUEST_SUFFIX = "attachment-import"
"""@brief 一个 Turn 内当前附件导入的稳定 request-ID 后缀 / Stable request-ID suffix for a current-Turn attachment import."""


@dataclass(frozen=True, slots=True)
class ImportedCurrentTurnAttachment:
    """@brief 已导入当前附件的模型安全投影 / Model-safe projection of an imported current attachment.

    @param path runtime 内唯一允许暴露的文件路径 / Sole runtime-internal path allowed to be exposed.
    @param byte_size 已核验并发布的字节数 / Verified and published byte count.
    @param sha256 已核验并发布的 SHA-256 / Verified and published SHA-256.
    @param replayed native 文件 journal 是否回放了既有收据 / Whether the native file journal replayed an existing receipt.
    @note ``path`` 不是 host path；该对象不携带 Telegram capability、文件名、MIME 或内容。
        ``path`` is not a host path; this object carries no Telegram capability, filename, MIME,
        or content.
    """

    path: str
    """@brief runtime 内固定文件路径 / Fixed file path inside the runtime."""
    byte_size: int
    """@brief 已发布的逻辑字节数 / Published logical byte count."""
    sha256: str
    """@brief 已发布内容的 SHA-256 / SHA-256 of published content."""
    replayed: bool
    """@brief 是否由 native journal 回放 / Whether native journal replayed the receipt."""

    def __post_init__(self) -> None:
        """@brief 校验应用层收据投影 / Validate the application receipt projection.

        @return None / None.
        @raise TypeError 字段类型不正确时抛出 / Raised when a field has an invalid type.
        @raise ValueError 路径、大小或摘要非法时抛出 / Raised when path, size, or digest is invalid.
        """

        # Reuse the application command/result invariant rather than maintaining another path
        # grammar in this use case.  The synthetic request ID is never persisted or exposed.
        AddFileResult(
            request_id=WorkspaceRequestId("attachment-projection"),
            replayed=self.replayed,
            path=self.path,
            byte_size=self.byte_size,
            sha256=self.sha256,
        )

    def model_placeholder(self) -> CanonicalMessage:
        """@brief 构造唯一模型可见的附件占位符 / Build the sole model-visible attachment placeholder.

        @return 仅含固定 workspace 路径的 user canonical message / User canonical message containing only the fixed workspace path.
        @note 故意不保留 caption、原始 filename、MIME 或 Telegram identity；这些是用户提供
            的非可信内容，且模型完成任务只需要 ``run_bash`` 可访问的路径。/ Caption,
            original filename, MIME, and Telegram identity are deliberately omitted: they are
            untrusted user-provided content and the model needs only the path available to
            ``run_bash`` to perform the task.
        """

        return text_message(
            MessageRole.USER,
            f'<workspace_file path="{self.path}" />',
        )


class CurrentTurnWorkspaceAttachmentPreprocessor:
    """@brief 将 durable 当前附件导入所属 Workspace / Import a durable current attachment into its owning Workspace.

    这是 application service（应用服务），仅协调两个窄端口：附件的内存下载与
``RuntimeProcess.add_file``。scope、目标路径、幂等 request ID 与 request hash 全部由已
接受的 durable command 派生，绝不由 Bot 模型或 Telegram 文件名决定。/ This application
service coordinates only two narrow ports: in-memory attachment download and
``RuntimeProcess.add_file``. Scope, target path, idempotent request ID, and request hash are all
derived from the accepted durable command, never from the Bot model or a Telegram filename.
    """

    def __init__(
        self,
        *,
        source: CurrentTurnUploadSource,
        runtime_process: RuntimeProcess,
    ) -> None:
        """@brief 注入下载端口与唯一 workspace 写入端口 / Inject the download port and sole Workspace write port.

        @param source 只下载当前 durable 引用附件的端口 / Port downloading only the current durable attachment reference.
        @param runtime_process 只暴露受控 runtime 能力的端口 / Port exposing only controlled runtime capabilities.
        @return None / None.
        @raise TypeError 端口缺失或不满足最小方法面时抛出 / Raised when a port is absent or lacks its minimal method surface.
        """

        if not callable(getattr(source, "download", None)):
            raise TypeError("Attachment preprocessor requires a CurrentTurnUploadSource")
        if not callable(getattr(runtime_process, "add_file", None)):
            raise TypeError("Attachment preprocessor requires a RuntimeProcess")
        self._source = source
        """@brief 当前附件的受限字节来源 / Bounded byte source for the current attachment."""
        self._runtime_process = runtime_process
        """@brief 唯一允许写入 workspace 的 application port / Sole application port allowed to write the Workspace."""

    async def preprocess(
        self,
        command: DurableAssistantInferenceCommand,
    ) -> ImportedCurrentTurnAttachment | None:
        """@brief 下载并导入当前 Turn 附件 / Download and import the current-Turn attachment.

        @param command 已由 durable worker 恢复并严格校验的命令 / Command restored and strictly validated by the durable worker.
        @return 没有附件时为 None；否则是模型安全文件投影 / None when no attachment exists; otherwise a model-safe file projection.
        @raise WorkspaceRuntimeProtocolError native receipt 与已下载内容或固定语义不一致时抛出 /
            Raised when a native receipt disagrees with downloaded content or fixed semantics.
        @note 这个方法不写宿主机文件、不启动 host shell，也不向 Agent runtime 传入下载 bytes。
            This method writes no host file, starts no host shell, and passes no download bytes to
            the Agent runtime.
        """

        if not isinstance(command, DurableAssistantInferenceCommand):
            raise TypeError(
                "Attachment preprocessing requires a DurableAssistantInferenceCommand"
            )
        reference = command.current_turn_upload
        if reference is None:
            return None

        downloaded = await self._source.download(reference)
        if not isinstance(downloaded, DownloadedCurrentTurnUpload):
            raise WorkspaceRuntimeProtocolError(
                "Current-turn upload source returned an invalid download result"
            )
        runtime_scope = _runtime_scope_for(command)
        opaque_id = workspace_attachment_opaque_id(
            turn_id=command.typed_turn_id,
            reference=reference,
        )
        request_id = _request_id_for(command.typed_turn_id)
        request_hash = _request_hash_for(
            scope=runtime_scope,
            turn_id=command.typed_turn_id,
            reference=reference,
            opaque_id=opaque_id,
            downloaded=downloaded,
        )
        add_file = AddFileCommand(
            scope=runtime_scope,
            opaque_id=opaque_id,
            chunks=_chunks(downloaded.content),
            byte_size=downloaded.byte_size,
            sha256=downloaded.sha256,
            request_id=request_id,
            request_hash=request_hash,
        )
        result = await self._runtime_process.add_file(add_file)
        _validate_native_receipt(
            result,
            command=add_file,
        )
        return ImportedCurrentTurnAttachment(
            path=result.path,
            byte_size=result.byte_size,
            sha256=result.sha256,
            replayed=result.replayed,
        )


def _runtime_scope_for(command: DurableAssistantInferenceCommand) -> RuntimeScope:
    """@brief 从 durable command 派生个人或整群 runtime scope / Derive a personal-or-whole-group runtime scope from a durable command.

    @param command 已校验 durable Assistant 命令 / Validated durable Assistant command.
    @return 个人用户或完整群聊的 runtime scope / Runtime scope for the personal user or whole group chat.
    @raise ValueError command 的群聊 ID 不可作为 runtime scope 时抛出 / Raised when a command group ID cannot serve as a runtime scope.
    @note Topic 不参与此映射；同一群全部 topic 共享一个 Workspace。/ Topics do not
        participate in this mapping; every topic in one group shares one Workspace.
    """

    if command.scope.is_group:
        group_id = command.scope.group_id
        if group_id is None:
            raise ValueError("Validated group command is missing its runtime group ID")
        return GroupRuntimeScope(group_id)
    return PersonalRuntimeScope(command.user.user_id)


def _request_id_for(turn_id: TurnId) -> WorkspaceRequestId:
    """@brief 派生当前附件导入的稳定 request ID / Derive the stable request ID for a current attachment import.

    @param turn_id 当前 durable Turn ID / Current durable Turn identifier.
    @return 用于 native journal 的稳定 request ID / Stable request ID for the native journal.
    """

    return WorkspaceRequestId(f"{turn_id}:{_ATTACHMENT_REQUEST_SUFFIX}")


def _request_hash_for(
    *,
    scope: RuntimeScope,
    turn_id: TurnId,
    reference: CurrentTurnUploadReference,
    opaque_id: str,
    downloaded: DownloadedCurrentTurnUpload,
) -> WorkspaceRequestHash:
    """@brief 为一次附件导入构造完整语义摘要 / Construct the complete semantic digest for one attachment import.

    @param scope 文件归属的强类型 runtime scope / Typed runtime scope owning the file.
    @param turn_id 当前 durable Turn ID / Current durable Turn identifier.
    @param reference 已接受的附件 identity / Accepted attachment identity.
    @param opaque_id 已派生且固定的目标目录 ID / Derived fixed target-directory ID.
    @param downloaded 已下载并摘要核验的 bytes 结果 / Downloaded and digest-verified byte result.
    @return 规范小写 SHA-256 request hash / Canonical lowercase SHA-256 request hash.
    @note 文件名、MIME 与 Telegram ``file_id`` 不参与幂等语义；内容摘要和 source identity
        才是导入事实。/ Filename, MIME, and Telegram ``file_id`` do not participate in
        idempotency semantics; content digest and source identity are the import fact.
    """

    scope_kind = "group" if isinstance(scope, GroupRuntimeScope) else "personal"
    payload = {
        "byte_size": downloaded.byte_size,
        "content_sha256": downloaded.sha256,
        "file_unique_id": reference.file_unique_id,
        "opaque_id": opaque_id,
        "operation": "current_turn_workspace_attachment_import",
        "scope_id": scope.stable_id,
        "scope_kind": scope_kind,
        "source_message_id": reference.source_message_id,
        "source_update_id": reference.source_update_id,
        "turn_id": str(turn_id),
        "upload_kind": reference.kind.value,
        "version": 1,
    }
    return WorkspaceRequestHash(_sha256_canonical_json(payload))


def _sha256_canonical_json(value: dict[str, object]) -> str:
    """@brief 计算固定 JSON 语义的 SHA-256 / Compute SHA-256 for stable JSON semantics.

    @param value 只含 JSON 原语的业务语义对象 / Business-semantics object containing JSON primitives only.
    @return UTF-8 canonical JSON 的小写 SHA-256 / Lowercase SHA-256 of UTF-8 canonical JSON.
    """

    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunks(content: bytes) -> Iterator[bytes]:
    """@brief 将内存附件切为 native 有界 chunks / Split an in-memory attachment into bounded native chunks.

    @param content 已核验的不可变附件 bytes / Verified immutable attachment bytes.
    @return 每块不超过 native 协议上限的惰性迭代器 / Lazy iterator whose chunks stay within the native protocol cap.
    @raise TypeError 内容不是不可变 bytes 时抛出 / Raised when content is not immutable bytes.
    """

    if not isinstance(content, bytes):
        raise TypeError("Attachment chunks require immutable bytes")
    for offset in range(0, len(content), MAX_ADD_FILE_CHUNK_BYTES):
        yield content[offset : offset + MAX_ADD_FILE_CHUNK_BYTES]


def _validate_native_receipt(
    result: object,
    *,
    command: AddFileCommand,
) -> None:
    """@brief 核验 native add_file 收据与导入意图一致 / Validate a native add_file receipt against the import intent.

    @param result native adapter 返回的候选结果 / Candidate result returned by the native adapter.
    @param command 已发送的固定文件导入命令 / Fixed file-import command that was sent.
    @return None / None.
    @raise WorkspaceRuntimeProtocolError 结果类型或任一受保护字段不一致时抛出 /
        Raised when result type or any protected field disagrees.
    """

    if not isinstance(result, AddFileResult):
        raise WorkspaceRuntimeProtocolError("wspctl add_file returned an invalid receipt")
    if (
        result.request_id != command.request_id
        or result.path != command.runtime_path
        or result.byte_size != command.byte_size
        or result.sha256 != command.sha256
    ):
        raise WorkspaceRuntimeProtocolError(
            "wspctl add_file receipt does not match the requested attachment import"
        )


__all__ = [
    "CurrentTurnWorkspaceAttachmentPreprocessor",
    "ImportedCurrentTurnAttachment",
]
