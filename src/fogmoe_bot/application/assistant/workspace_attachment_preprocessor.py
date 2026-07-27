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

from fogmoe_bot.application.workspace.errors import (
    WorkspaceFileReplayNotFoundError,
    WorkspaceRuntimeProtocolError,
)
from fogmoe_bot.application.workspace.models import (
    MAX_ADD_FILE_CHUNK_BYTES,
    AddFileCommand,
    AddFileResult,
    ReplayFileCommand,
)
from fogmoe_bot.application.workspace.ports import RuntimeProcess
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.conversation.identity import (
    CURRENT_USER_MESSAGE_SEMANTIC_KEY,
    ConversationMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.workspace.attachment import AttachmentImportIntent
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
from .workspace_attachment_receipt import (
    ConversationHistoryInvalidator,
    WorkspaceAttachmentImportReceipt,
    WorkspaceAttachmentReceiptUnavailableError,
    WorkspaceAttachmentReceiptStore,
)
from .workspace_attachment_intent import (
    WorkspaceAttachmentImportIntentStore,
    WorkspaceAttachmentIntentConflictError,
)

_ATTACHMENT_REQUEST_SUFFIX = "attachment-import"
"""@brief 一个 Turn 内当前附件导入的稳定 request-ID 后缀 / Stable request-ID suffix for a current-Turn attachment import."""


@dataclass(frozen=True, slots=True)
class ImportedCurrentTurnAttachment:
    """@brief 已导入当前附件的模型安全投影 / Model-safe projection of an imported current attachment.

    @param receipt 已原子见证的 native 导入事实 / Atomically witnessed native import fact.
    @param replayed native 文件 journal 是否回放了既有收据 / Whether the native file journal replayed an existing receipt.
    @note ``receipt.path`` 不是 host path；该对象不携带 Telegram capability、文件名、MIME
        或内容。/ ``receipt.path`` is not a host path; this object carries no Telegram
        capability, filename, MIME, or content.
    """

    receipt: WorkspaceAttachmentImportReceipt
    """@brief 已持久化 publish 语义的导入 receipt / Import receipt carrying persisted publish semantics."""
    replayed: bool
    """@brief 是否由 native journal 回放 / Whether native journal replayed the receipt."""

    def __post_init__(self) -> None:
        """@brief 校验应用层收据投影 / Validate the application receipt projection.

        @return None / None.
        @raise TypeError 字段类型不正确时抛出 / Raised when a field has an invalid type.
        @raise ValueError 路径、大小或摘要非法时抛出 / Raised when path, size, or digest is invalid.
        """

        if not isinstance(self.receipt, WorkspaceAttachmentImportReceipt):
            raise TypeError(
                "Imported attachment requires a WorkspaceAttachmentImportReceipt"
            )
        if not isinstance(self.replayed, bool):
            raise TypeError("Imported attachment replayed must be a bool")

    @property
    def path(self) -> str:
        """@brief 返回唯一允许进入模型的 runtime 路径 / Return the sole runtime path allowed to enter the model.

        @return Workspace 内已见证 payload 路径 / Witnessed payload path inside the Workspace.
        """

        return self.receipt.path

    @property
    def byte_size(self) -> int:
        """@brief 返回已见证的字节数 / Return the witnessed byte count.

        @return 已核验 payload 字节数 / Verified payload byte count.
        """

        return self.receipt.byte_size

    @property
    def sha256(self) -> str:
        """@brief 返回已见证的内容摘要 / Return the witnessed content digest.

        @return 规范小写 SHA-256 / Canonical lowercase SHA-256.
        """

        return self.receipt.sha256

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

        这是 application service（应用服务），协调四个窄端口：附件的内存下载、意图准备、
    ``RuntimeProcess`` 的只读 replay/受控 ``add_file`` 与 receipt publish。scope、目标路径、
    幂等 request ID 与 request hash 全部由已接受的 durable command 派生，绝不由 Bot 模型或
    Telegram 文件名决定。/ This application service coordinates four narrow ports: in-memory
    attachment download, intent preparation, ``RuntimeProcess`` read-only replay/controlled
    ``add_file``, and receipt publication. Scope, target path, idempotent request ID, and request
    hash are all derived from the accepted durable command, never from the Bot model or a Telegram
    filename.
    """

    def __init__(
        self,
        *,
        source: CurrentTurnUploadSource,
        runtime_process: RuntimeProcess,
        intents: WorkspaceAttachmentImportIntentStore,
        receipts: WorkspaceAttachmentReceiptStore,
        history_invalidator: ConversationHistoryInvalidator,
    ) -> None:
        """@brief 注入下载端口与唯一 workspace 写入端口 / Inject the download port and sole Workspace write port.

        @param source 只下载当前 durable 引用附件的端口 / Port downloading only the current durable attachment reference.
        @param runtime_process 只暴露受控 runtime 能力的端口 / Port exposing only controlled runtime capabilities.
        @param intents 在 native 副作用前保存 immutable 导入意图的端口 /
            Port persisting the immutable import intent before the native side effect.
        @param receipts 将 native 成功原子发布到 Conversation 的 receipt 端口 /
            Receipt port atomically publishing native success into Conversation.
        @param history_invalidator receipt 发布后失效本地会话历史投影的端口 /
            Port invalidating local conversation-history projections after receipt publication.
        @return None / None.
        @raise TypeError 端口缺失或不满足最小方法面时抛出 / Raised when a port is absent or lacks its minimal method surface.
        """

        if not callable(getattr(source, "download", None)):
            raise TypeError(
                "Attachment preprocessor requires a CurrentTurnUploadSource"
            )
        if not callable(getattr(runtime_process, "add_file", None)) or not callable(
            getattr(runtime_process, "replay_file", None)
        ):
            raise TypeError("Attachment preprocessor requires a RuntimeProcess")
        if not callable(getattr(intents, "find", None)) or not callable(
            getattr(intents, "prepare", None)
        ):
            raise TypeError(
                "Attachment preprocessor requires a WorkspaceAttachmentImportIntentStore"
            )
        if not callable(getattr(receipts, "record_import", None)):
            raise TypeError(
                "Attachment preprocessor requires a WorkspaceAttachmentReceiptStore"
            )
        if not callable(getattr(history_invalidator, "invalidate", None)):
            raise TypeError(
                "Attachment preprocessor requires a ConversationHistoryInvalidator"
            )
        self._source = source
        """@brief 当前附件的受限字节来源 / Bounded byte source for the current attachment."""
        self._runtime_process = runtime_process
        """@brief 唯一允许写入 workspace 的 application port / Sole application port allowed to write the Workspace."""
        self._intents = intents
        """@brief native 调用前的 immutable intent aggregate store / Immutable intent aggregate store before native invocation."""
        self._receipts = receipts
        """@brief native 发布与模型可见性之间的原子见证端口 / Atomic witness port between native publication and model visibility."""
        self._history_invalidator = history_invalidator
        """@brief receipt 后本地 Context Window 失效端口 / Local Context Window invalidation port after a receipt."""

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

        # A prepared intent is the recovery bridge over an expiring Telegram capability.  It is
        # deliberately queried before a download: a completed native journal means no provider
        # byte access is needed to finish receipt publication.  prepared intent 是跨越会过期
        # Telegram capability 的恢复桥；必须先查它，已完成 native journal 时无需再取 provider bytes。
        intent = await self._intents.find(command)
        downloaded: DownloadedCurrentTurnUpload | None = None
        if intent is None:
            downloaded = await _download_current_turn_upload(self._source, reference)
            intent = await self._intents.prepare(
                command,
                _intent_for_download(
                    command=command,
                    reference=reference,
                    downloaded=downloaded,
                ),
            )
        intent = _validate_intent_for_command(
            intent,
            command=command,
            reference=reference,
        )

        replay = ReplayFileCommand(
            scope=intent.scope,
            opaque_id=intent.opaque_id,
            byte_size=intent.byte_size,
            sha256=intent.sha256,
            request_id=intent.request_id,
            request_hash=intent.request_hash,
        )
        try:
            result = await self._runtime_process.replay_file(replay)
        except WorkspaceFileReplayNotFoundError:
            # ``not_found`` is the only native answer that proves there is no completed or
            # pending side effect.  Only this branch may re-use the Telegram capability.  只有
            # 明确 not_found 才证明没有 completed/pending 副作用，因此唯有该分支可再次下载。
            if downloaded is None:
                downloaded = await _download_current_turn_upload(
                    self._source, reference
                )
            _validate_download_matches_intent(
                downloaded,
                intent=intent,
                reference=reference,
            )
            add_file = AddFileCommand(
                scope=intent.scope,
                opaque_id=intent.opaque_id,
                chunks=_chunks(downloaded.content),
                byte_size=intent.byte_size,
                sha256=intent.sha256,
                request_id=intent.request_id,
                request_hash=intent.request_hash,
            )
            result = await self._runtime_process.add_file(add_file)
            _validate_native_receipt(result, command=add_file)
        else:
            _validate_native_receipt(result, command=replay, require_replayed=True)
        receipt = WorkspaceAttachmentImportReceipt(
            turn_id=command.typed_turn_id,
            conversation_id=command.typed_conversation_id,
            scope=intent.scope,
            request_id=intent.request_id,
            request_hash=intent.request_hash,
            path=result.path,
            byte_size=result.byte_size,
            sha256=result.sha256,
        )
        # The native journal can safely replay after a DB interruption, but the Agent may only
        # see the path after this second, transactional witness succeeds.  native journal 在 DB
        # 中断后可安全回放，但只有这第二个事务性见证成功后 Agent 才能看到路径。
        await self._receipts.record_import(command, receipt)
        try:
            self._history_invalidator.invalidate(command.typed_conversation_id)
        except Exception as error:
            # Do not let this attempt use a stale pending cache after the receipt is durable.
            # A retry is safe: native replays the file journal and receipt publication is
            # idempotent. receipt 已持久化后，本次不能继续使用 stale pending cache；重试会
            # 安全回放 native journal，而 receipt publish 是幂等的。
            raise WorkspaceAttachmentReceiptUnavailableError(
                "Conversation history cache invalidation failed after attachment receipt"
            ) from error
        return ImportedCurrentTurnAttachment(
            receipt=receipt,
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


async def _download_current_turn_upload(
    source: CurrentTurnUploadSource,
    reference: CurrentTurnUploadReference,
) -> DownloadedCurrentTurnUpload:
    """@brief 下载并核验当前授权附件的 in-memory 结果 / Download and validate the in-memory result for the currently authorized attachment.

    @param source 只允许当前 durable reference 的下载端口 / Download port allowing only the current durable reference.
    @param reference 已接受的 provider capability 引用 / Accepted provider-capability reference.
    @return 严格类型化、内容已自校验的下载结果 / Strictly typed download result whose content self-validates.
    @raise WorkspaceRuntimeProtocolError source 返回了不符合端口契约的对象时抛出 /
        Raised when the source returns an object violating the port contract.
    """

    downloaded = await source.download(reference)
    if not isinstance(downloaded, DownloadedCurrentTurnUpload):
        raise WorkspaceRuntimeProtocolError(
            "Current-turn upload source returned an invalid download result"
        )
    return downloaded


def _intent_for_download(
    *,
    command: DurableAssistantInferenceCommand,
    reference: CurrentTurnUploadReference,
    downloaded: DownloadedCurrentTurnUpload,
) -> AttachmentImportIntent:
    """@brief 从一次已校验下载构造待准备的聚合 / Construct the aggregate to prepare from one verified download.

    @param command 已验证 durable Assistant command / Validated durable Assistant command.
    @param reference command 当前唯一授权的附件引用 / The command's sole authorized attachment reference.
    @param downloaded 已自校验的内存内容结果 / Self-validating in-memory content result.
    @return 尚待存储端口原子准备的不可变意图 / Immutable intent still awaiting atomic preparation by the store port.
    """

    runtime_scope = _runtime_scope_for(command)
    opaque_id = workspace_attachment_opaque_id(
        turn_id=command.typed_turn_id,
        reference=reference,
    )
    return AttachmentImportIntent(
        turn_id=command.typed_turn_id,
        conversation_id=command.typed_conversation_id,
        source_message_id=ConversationMessageId.for_turn(
            command.typed_turn_id,
            CURRENT_USER_MESSAGE_SEMANTIC_KEY,
        ),
        scope=runtime_scope,
        opaque_id=opaque_id,
        request_id=_request_id_for(command.typed_turn_id),
        request_hash=_request_hash_for(
            scope=runtime_scope,
            turn_id=command.typed_turn_id,
            reference=reference,
            opaque_id=opaque_id,
            downloaded=downloaded,
        ),
        byte_size=downloaded.byte_size,
        sha256=downloaded.sha256,
    )


def _validate_intent_for_command(
    intent: object,
    *,
    command: DurableAssistantInferenceCommand,
    reference: CurrentTurnUploadReference,
) -> AttachmentImportIntent:
    """@brief 验证已准备意图仍精确属于当前 durable source / Validate that a prepared intent still belongs exactly to the current durable source.

    @param intent 从 intent store 读取或准备后的候选 aggregate / Candidate aggregate read or prepared by the intent store.
    @param command 已验证 durable Assistant command / Validated durable Assistant command.
    @param reference command 当前唯一授权的附件引用 / The command's sole authorized attachment reference.
    @return 经验证的 immutable intent / Validated immutable intent.
    @raise WorkspaceRuntimeProtocolError store 返回非 aggregate 对象时抛出 / Raised when the store returns a non-aggregate object.
    @raise WorkspaceAttachmentIntentConflictError turn、source message、scope、path 或 request ID 漂移时抛出 /
        Raised when Turn, source message, scope, path, or request ID drifts.
    """

    if not isinstance(intent, AttachmentImportIntent):
        raise WorkspaceRuntimeProtocolError(
            "Attachment intent store returned an invalid import intent"
        )
    expected_scope = _runtime_scope_for(command)
    expected_opaque_id = workspace_attachment_opaque_id(
        turn_id=command.typed_turn_id,
        reference=reference,
    )
    expected_source_message_id = ConversationMessageId.for_turn(
        command.typed_turn_id,
        CURRENT_USER_MESSAGE_SEMANTIC_KEY,
    )
    if (
        intent.turn_id != command.typed_turn_id
        or intent.conversation_id != command.typed_conversation_id
        or intent.source_message_id != expected_source_message_id
        or intent.scope != expected_scope
        or intent.opaque_id != expected_opaque_id
        or intent.request_id != _request_id_for(command.typed_turn_id)
    ):
        raise WorkspaceAttachmentIntentConflictError(
            "Attachment import intent conflicts with its durable source"
        )
    return intent


def _validate_download_matches_intent(
    downloaded: DownloadedCurrentTurnUpload,
    *,
    intent: AttachmentImportIntent,
    reference: CurrentTurnUploadReference,
) -> None:
    """@brief 确认在 not-found 后重新下载的 bytes 精确匹配 immutable intent / Confirm that bytes re-downloaded after not-found exactly match the immutable intent.

    @param downloaded 重新取得且已自校验的内容 / Reacquired, self-validating content.
    @param intent 已持久化、不可变的导入 aggregate / Persisted immutable import aggregate.
    @param reference 当前 durable provider 引用 / Current durable provider reference.
    @return None / None.
    @raise WorkspaceAttachmentIntentConflictError 大小、内容摘要或完整 request hash 不一致时抛出 /
        Raised when byte size, content digest, or complete request hash differs.
    @note 这条比较禁止 provider 在重试间静默替换相同 capability 的内容；不能匹配时也不
        能改写 intent。/ This comparison prevents a provider from silently replacing bytes for
        the same capability across retries; a mismatch cannot rewrite the intent.
    """

    expected_request_hash = _request_hash_for(
        scope=intent.scope,
        turn_id=intent.turn_id,
        reference=reference,
        opaque_id=intent.opaque_id,
        downloaded=downloaded,
    )
    if (
        downloaded.byte_size != intent.byte_size
        or downloaded.sha256 != intent.sha256
        or expected_request_hash != intent.request_hash
    ):
        raise WorkspaceAttachmentIntentConflictError(
            "Re-downloaded attachment does not match its immutable import intent"
        )


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
    command: AddFileCommand | ReplayFileCommand,
    require_replayed: bool = False,
) -> None:
    """@brief 核验 native add_file 收据与导入意图一致 / Validate a native add_file receipt against the import intent.

    @param result native adapter 返回的候选结果 / Candidate result returned by the native adapter.
    @param command 已发送的固定文件导入或只读 replay 命令 / Fixed file-import or read-only replay command that was sent.
    @param require_replayed 为 True 时要求 native 明确标记为 completed journal replay /
        When True, require native to mark the result as a completed journal replay.
    @return None / None.
    @raise WorkspaceRuntimeProtocolError 结果类型或任一受保护字段不一致时抛出 /
        Raised when result type or any protected field disagrees.
    """

    if not isinstance(result, AddFileResult):
        raise WorkspaceRuntimeProtocolError(
            "wspctl add_file returned an invalid receipt"
        )
    if require_replayed and result.replayed is not True:
        raise WorkspaceRuntimeProtocolError(
            "wspctl replay_file returned a non-replayed receipt"
        )
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
