"""@brief 当前 Turn 附件预处理到 Workspace 的 CTest / CTest for current-Turn attachment preprocessing into Workspace."""

from __future__ import annotations

import asyncio
import hashlib
import unittest
from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.assistant.agent_loop import AgentResponse
from fogmoe_bot.application.assistant.current_turn_upload import (
    CurrentTurnUploadDownloadError,
    CurrentTurnUploadIntegrityError,
    CurrentTurnUploadReference,
    CurrentTurnUploadTooLargeError,
    CurrentTurnUploadTransportError,
    CurrentTurnUploadUnavailableError,
    DownloadedCurrentTurnUpload,
    workspace_attachment_file_path,
)
from fogmoe_bot.application.assistant.durable_inference import (
    DurableAssistantInferenceAdapter,
)
from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
)
from fogmoe_bot.application.assistant.workspace_attachment_preprocessor import (
    CurrentTurnWorkspaceAttachmentPreprocessor,
    WorkspaceAttachmentImportPendingError,
)
from fogmoe_bot.application.assistant.workspace_attachment_intent import (
    WorkspaceAttachmentIntentConflictError,
)
from fogmoe_bot.application.assistant.workspace_attachment_receipt import (
    WorkspaceAttachmentImportReceipt,
    WorkspaceAttachmentReceiptUnavailableError,
)
from fogmoe_bot.application.context_window.projection import (
    ContextWindowBounds,
    ContextWindowReady,
    ContextWindowRequest,
    ContextWindowResult,
)
from fogmoe_bot.application.conversation.inference_worker import (
    InferenceDependencyPending,
    InferenceErrorCategory,
    InferenceRuntimeLimits,
    PermanentInferenceError,
    RetryableInferenceError,
)
from fogmoe_bot.application.workspace.errors import (
    WorkspaceFileReplayNotFoundError,
    WorkspaceRuntimeProtocolError,
    WorkspaceRuntimeUnavailableError,
)
from fogmoe_bot.application.workspace.models import (
    MAX_ADD_FILE_CHUNK_BYTES,
    AddFileCommand,
    AddFileResult,
    ReplayFileCommand,
)
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.assistant.request_metadata import RequestMeta
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.domain.context_window.budget import TokenCount
from fogmoe_bot.domain.conversation.identity import (
    ConversationMessageId,
    MessageSequence,
    TurnId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.workspace.scope import GroupRuntimeScope, PersonalRuntimeScope
from fogmoe_bot.domain.workspace.attachment import (
    AttachmentImportIntent,
    pending_workspace_attachment_marker,
)

_NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 测试使用的稳定 UTC 时间 / Stable UTC time used by tests."""

_CONTENT = b"#!/bin/sh\nprintf '%s\\n' workspace-only\n"
"""@brief 表明内容可能在 Workspace 执行、但绝不由 host 解释的测试 bytes / Test bytes that may execute in Workspace but are never interpreted by the host."""


class _UploadSource:
    """@brief 记录当前 Document 下载引用的 source 替身 / Source double recording current-Document download references."""

    def __init__(self, content: bytes = _CONTENT) -> None:
        """@brief 绑定待下载内容 / Bind the content to download.

        @param content 将返回的不可变 bytes / Immutable bytes to return.
        @return None / None.
        """

        self._content = content
        self.references: list[CurrentTurnUploadReference] = []

    async def download(
        self,
        reference: CurrentTurnUploadReference,
    ) -> DownloadedCurrentTurnUpload:
        """@brief 记录唯一的 durable 引用并返回核验 bytes / Record the sole durable reference and return verified bytes.

        @param reference 当前 durable command 授权的引用 / Reference authorized by the current durable command.
        @return 已校验内存 bytes / Verified in-memory bytes.
        """

        self.references.append(reference)
        return DownloadedCurrentTurnUpload(
            content=self._content,
            byte_size=len(self._content),
            sha256=hashlib.sha256(self._content).hexdigest(),
            original_file_name=reference.original_file_name,
            mime_type=reference.mime_type,
        )


class _FailingUploadSource:
    """@brief 抛出指定下载失败的 source 替身 / Source double raising one configured download failure.

    @param error 每次 download 时抛出的受控异常 / Controlled exception raised by every download.
    """

    def __init__(self, error: Exception) -> None:
        """@brief 保存测试错误 / Store the test error.

        @param error 当前附件下载应抛出的异常 / Exception that current-attachment download must raise.
        @return None / None.
        """

        self._error = error

    async def download(
        self,
        reference: CurrentTurnUploadReference,
    ) -> DownloadedCurrentTurnUpload:
        """@brief 证明 durable adapter 的失败分类不依赖 Telegram SDK / Prove durable failure classification is independent of the Telegram SDK.

        @param reference 已授权当前附件引用 / Authorized current-attachment reference.
        @return 永不返回 / Never returns.
        """

        del reference
        raise self._error


class _ExpiringUploadSource(_UploadSource):
    """@brief 首次返回附件、之后模拟 provider capability 过期的 source / Source returning an attachment once and then simulating an expired provider capability."""

    async def download(
        self,
        reference: CurrentTurnUploadReference,
    ) -> DownloadedCurrentTurnUpload:
        """@brief 只允许首次下载；后续调用明确失败 / Allow only the first download and explicitly fail later calls.

        @param reference 当前 durable 附件引用 / Current durable attachment reference.
        @return 首次的已验证内容 / Verified content on the first call.
        @raise CurrentTurnUploadUnavailableError 第二次调用表示 capability 已过期 / Raised on a second call to represent an expired capability.
        """

        if self.references:
            raise CurrentTurnUploadUnavailableError(
                "test Telegram capability expired after the first download"
            )
        return await super().download(reference)


class _RuntimeProcess:
    """@brief 以 completed journal 模拟 add_file/replay_file 的 RuntimeProcess 替身 / RuntimeProcess double simulating add_file/replay_file through a completed journal."""

    def __init__(self, *, mismatched_receipt: bool = False) -> None:
        """@brief 配置收据是否故意漂移 / Configure whether the receipt deliberately drifts.

        @param mismatched_receipt 为 True 时返回错误 path / When True, return a wrong path.
        @return None / None.
        """

        self.mismatched_receipt = mismatched_receipt
        self.commands: list[AddFileCommand] = []
        self.replay_commands: list[ReplayFileCommand] = []
        self.chunk_payloads: list[bytes] = []
        self.chunk_lengths: list[tuple[int, ...]] = []
        self._completed: dict[str, AddFileResult] = {}

    async def replay_file(self, command: ReplayFileCommand) -> AddFileResult:
        """@brief 回放 completed journal，未命中时明确 not_found / Replay a completed journal or explicitly report not-found.

        @param command 只读 journal 查询 / Read-only journal lookup.
        @return 已完成文件的 replay receipt / Replay receipt of the completed file.
        @raise WorkspaceFileReplayNotFoundError 尚未执行 add_file 时抛出 / Raised before add_file has executed.
        """

        self.replay_commands.append(command)
        completed = self._completed.get(command.request_id.value)
        if completed is None:
            raise WorkspaceFileReplayNotFoundError(command.request_id)
        if (
            completed.path != command.runtime_path
            or completed.byte_size != command.byte_size
            or completed.sha256 != command.sha256
        ):
            raise AssertionError("test completed journal drifted from replay command")
        return AddFileResult(
            request_id=command.request_id,
            replayed=True,
            path=completed.path,
            byte_size=completed.byte_size,
            sha256=completed.sha256,
        )

    async def add_file(self, command: AddFileCommand) -> AddFileResult:
        """@brief 消费一次 command stream 并返回 native 风格收据 / Consume one command stream and return a native-style receipt.

        @param command 待导入文件的应用命令 / Application command importing the file.
        @return 匹配或故意不匹配的 native 收据 / Matching or deliberately mismatched native receipt.
        """

        chunks = tuple(command.chunks)
        self.commands.append(command)
        self.chunk_payloads.append(b"".join(chunks))
        self.chunk_lengths.append(tuple(len(chunk) for chunk in chunks))
        path = command.runtime_path
        if self.mismatched_receipt:
            path = "/workspace/uploads/other/payload"
        result = AddFileResult(
            request_id=command.request_id,
            replayed=False,
            path=path,
            byte_size=command.byte_size,
            sha256=command.sha256,
        )
        self._completed[command.request_id.value] = result
        return result


class _FailingReplayRuntime(_RuntimeProcess):
    """@brief 在只读 journal replay 阶段持续失败的 runtime 替身 / Runtime double persistently failing during read-only journal replay.

    @param error replay 应抛出的受控错误 / Controlled error raised by replay.
    """

    def __init__(self, error: Exception) -> None:
        """@brief 保存固定 replay 错误 / Store the fixed replay error.

        @param error 每次 replay 抛出的异常 / Exception raised by every replay.
        @return None / None.
        """

        super().__init__()
        self._error = error
        """@brief 固定的恢复依赖故障 / Fixed recovery-dependency failure."""

    async def replay_file(self, command: ReplayFileCommand) -> AddFileResult:
        """@brief 记录 replay 后抛出固定错误 / Record replay and raise the fixed error.

        @param command 绑定已提交 intent 的 replay 命令 / Replay command bound to the committed intent.
        @return 永不返回 / Never returns.
        """

        self.replay_commands.append(command)
        raise self._error


class _IntentStore:
    """@brief 内存中的 immutable AttachmentImportIntent aggregate store / In-memory immutable AttachmentImportIntent aggregate store."""

    def __init__(self) -> None:
        """@brief 初始化空 intent journal / Initialize an empty intent journal.

        @return None / None.
        """

        self.intents: dict[TurnId, AttachmentImportIntent] = {}
        self.prepared: list[AttachmentImportIntent] = []

    async def find(
        self,
        command: DurableAssistantInferenceCommand,
    ) -> AttachmentImportIntent | None:
        """@brief 返回同 Turn 已准备 intent / Return a prepared intent for the same Turn.

        @param command 当前 durable command / Current durable command.
        @return 已存 aggregate 或 None / Stored aggregate or None.
        """

        return self.intents.get(command.typed_turn_id)

    async def prepare(
        self,
        command: DurableAssistantInferenceCommand,
        intent: AttachmentImportIntent,
    ) -> AttachmentImportIntent:
        """@brief 首次保存或验证并发等价 intent / Store the first intent or validate an equivalent concurrent intent.

        @param command 当前 durable command / Current durable command.
        @param intent 候选 immutable aggregate / Candidate immutable aggregate.
        @return 同 Turn 的唯一 aggregate / Sole aggregate for the Turn.
        """

        existing = self.intents.get(command.typed_turn_id)
        if existing is not None:
            if existing != intent:
                raise WorkspaceAttachmentIntentConflictError(
                    "test intent store rejected mutable replay semantics"
                )
            return existing
        self.intents[command.typed_turn_id] = intent
        self.prepared.append(intent)
        return intent


class _ReceiptStore:
    """@brief 记录 native 成功后的 receipt publish 的 durable 替身 / Durable double recording receipt publication after native success."""

    def __init__(
        self,
        error: Exception | None = None,
        *,
        failures_remaining: int | None = None,
    ) -> None:
        """@brief 配置可选 publish 失败 / Configure an optional publication failure.

        @param error publish 应抛出的可选错误 / Optional error raised by publication.
        @param failures_remaining 在成功前应注入错误的次数；None 表示每次都使用 ``error`` /
            Number of injected failures before success; None means use ``error`` every time.
        @return None / None.
        """

        self._error = error
        self._failures_remaining = failures_remaining
        self.records: list[
            tuple[DurableAssistantInferenceCommand, WorkspaceAttachmentImportReceipt]
        ] = []
        self.published: list[
            tuple[DurableAssistantInferenceCommand, WorkspaceAttachmentImportReceipt]
        ] = []

    async def record_import(
        self,
        command: DurableAssistantInferenceCommand,
        receipt: WorkspaceAttachmentImportReceipt,
    ) -> None:
        """@brief 记录或拒绝一个 receipt publish / Record or reject one receipt publication.

        @param command 已恢复的 durable command / Restored durable command.
        @param receipt native 成功导出的 receipt / Receipt derived from native success.
        @return None / None.
        """

        self.records.append((command, receipt))
        if self._error is not None and self._failures_remaining is None:
            raise self._error
        if self._failures_remaining is not None and self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise self._error or RuntimeError("test receipt failure")
        self.published.append((command, receipt))


class _HistoryInvalidator:
    """@brief 记录 receipt 后历史失效的 Context Window 替身 / Context-Window double recording post-receipt history invalidation."""

    def __init__(self, error: Exception | None = None) -> None:
        """@brief 配置可选失效错误 / Configure an optional invalidation error.

        @param error 每次 invalidate 应抛出的可选错误 / Optional error raised by every invalidation.
        @return None / None.
        """

        self._error = error
        self.conversations: list[object] = []

    def invalidate(self, conversation_id: object) -> None:
        """@brief 记录一个会话失效 / Record one conversation invalidation.

        @param conversation_id receipt 已发布的会话 / Conversation whose receipt was published.
        @return None / None.
        """

        self.conversations.append(conversation_id)
        if self._error is not None:
            raise self._error


class _History:
    """@brief 返回一个当前 Turn anchor 的 history projector 替身 / History-projector double returning a current-Turn anchor."""

    def __init__(self, message: ConversationMessage) -> None:
        """@brief 保存唯一 anchor 消息 / Store the sole anchor message.

        @param message 当前 Turn 的规范 user row / Canonical current-Turn user row.
        @return None / None.
        """

        self._message = message
        self.requests: list[ContextWindowRequest] = []

    async def project(self, request: ContextWindowRequest) -> ContextWindowResult:
        """@brief 返回含唯一 user canonical message 的 ready projection / Return a ready projection containing one user canonical message.

        @param request 当前 adapter 的历史请求 / Current history request from the adapter.
        @return 可直接调用模型的投影 / Projection ready for a model call.
        """

        self.requests.append(request)
        model_message = self._message.draft.content["model_message"]
        if not isinstance(model_message, dict):
            raise AssertionError("test model_message must be JSON object")
        return ContextWindowReady(
            checkpoint_summary=None,
            messages=(CanonicalMessage.from_json(model_message),),
            estimated_tokens=TokenCount(1),
            bounds=ContextWindowBounds(
                request.conversation_id,
                request.through_turn_id,
                1,
                1,
                0,
            ),
            checkpoint=None,
            anchor_messages=(self._message,),
        )


class _Inference:
    """@brief 捕获模型可见 ContextState 的 Assistant inference 替身 / Assistant-inference double capturing the model-visible ContextState."""

    def __init__(self) -> None:
        """@brief 初始化空 ContextState 槽 / Initialize an empty ContextState slot."""

        self.context: ContextState | None = None

    async def infer(
        self,
        context_state: ContextState,
        *,
        allow_tools: bool = True,
        request_timeout: float | None = None,
        request_meta: RequestMeta | None = None,
        tool_context: object | None = None,
    ) -> AgentResponse:
        """@brief 记录 ContextState 并返回可提交文本 / Record ContextState and return committable text.

        @param context_state 模型将看到的 canonical 上下文 / Canonical context the model will see.
        @param allow_tools 是否允许工具 / Whether tools are allowed.
        @param request_timeout 推理请求超时 / Inference request timeout.
        @param request_meta 显式 metadata / Explicit metadata.
        @param tool_context durable 工具身份 / Durable tool identity.
        @return 最小 Agent 响应 / Minimal Agent response.
        """

        del allow_tools, request_timeout, request_meta, tool_context
        self.context = context_state
        return AgentResponse("done", ())


def _reference() -> CurrentTurnUploadReference:
    """@brief 构造带危险展示元数据的 durable Document 引用 / Build a durable Document reference with hostile-looking display metadata.

    @return 已接受的当前 Turn Document 引用 / Accepted current-Turn Document reference.
    """

    return CurrentTurnUploadReference(
        file_id="telegram-capability-never-model-visible",
        file_unique_id="telegram-stable-identity-never-path",
        source_update_id=101,
        source_message_id=7,
        declared_byte_size=len(_CONTENT),
        original_file_name="../../not-a-host-path.sh",
        mime_type="application/x-shellscript",
    )


def _command(
    *,
    upload: CurrentTurnUploadReference | None = None,
    is_group: bool = False,
) -> DurableAssistantInferenceCommand:
    """@brief 构造当前附件预处理所需的严格 durable command / Build a strict durable command required by attachment preprocessing.

    @param upload 可选当前 Turn Document 引用 / Optional current-Turn Document reference.
    @param is_group 是否构造群聊 scope / Whether to construct a group scope.
    @return 已校验 durable command / Validated durable command.
    """

    turn_id = TurnId.new()
    if is_group:
        return DurableAssistantInferenceCommand(
            schema_version=2,
            conversation_id="assistant-group:-10042:thread:9",
            turn_id=str(turn_id),
            delivery_stream_id="telegram:primary:chat:-10042:thread:9",
            chat_id=-10042,
            reply_to_message_id=7,
            message_thread_id=9,
            user=DurableAssistantUser(
                user_id=42,
                username="klee",
                display_name="Klee",
                coins=0,
                plan=AccountPlan.FREE,
                permission=0,
            ),
            scope=DurableAssistantScope(
                is_group=True,
                group_id=-10042,
                message_id=7,
                message_thread_id=9,
            ),
            current_turn_upload=upload,
        )
    return DurableAssistantInferenceCommand(
        schema_version=2,
        conversation_id="assistant-user:42",
        turn_id=str(turn_id),
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


def _anchor(command: DurableAssistantInferenceCommand) -> ConversationMessage:
    """@brief 为 command 构造持久化当前 user anchor / Build the persisted current-user anchor for a command.

    @param command 当前 strict durable command / Current strict durable command.
    @return 正好一条可投影 user row / Exactly one projectable user row.
    """

    original_text = "caption that must not reach the model"
    if command.current_turn_upload is not None:
        original_text = (
            '<workspace_file path="'
            + workspace_attachment_file_path(
                turn_id=command.typed_turn_id,
                reference=command.current_turn_upload,
            )
            + '" />'
        )
    content: JsonObject = {
        "text": original_text,
        "model_message": text_message(MessageRole.USER, original_text).to_json(),
    }
    if command.current_turn_upload is not None:
        content["workspace_attachment"] = pending_workspace_attachment_marker()
    return ConversationMessage(
        draft=MessageDraft(
            message_id=ConversationMessageId.for_turn(
                command.typed_turn_id,
                "current-user",
            ),
            conversation_id=command.typed_conversation_id,
            turn_id=command.typed_turn_id,
            source_update_id=UpdateId(101),
            role=MessageRole.USER,
            content=content,
            idempotency_key="current-user",
            created_at=_NOW,
        ),
        sequence=MessageSequence(1),
    )


class WorkspaceAttachmentPreprocessorTests(unittest.TestCase):
    """@brief 附件→RuntimeProcess→模型占位符的不变量 / Invariants from attachment through RuntimeProcess to model placeholder."""

    def test_preprocess_uses_runtime_process_and_exposes_only_fixed_path(self) -> None:
        """@brief 导入只经 RuntimeProcess，模型投影只有固定路径 / Import uses only RuntimeProcess and the model projection has only a fixed path.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行 private attachment import / Run a private attachment import.

            @return None / None.
            """

            source = _UploadSource()
            runtime = _RuntimeProcess()
            command = _command(upload=_reference())
            result = await CurrentTurnWorkspaceAttachmentPreprocessor(
                source=source,
                runtime_process=runtime,  # type: ignore[arg-type]
                intents=_IntentStore(),
                receipts=_ReceiptStore(),
                history_invalidator=_HistoryInvalidator(),
            ).preprocess(command)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(source.references, [command.current_turn_upload])
            self.assertEqual(len(runtime.commands), 1)
            add_file = runtime.commands[0]
            self.assertIsInstance(add_file.scope, PersonalRuntimeScope)
            self.assertEqual(add_file.scope, PersonalRuntimeScope(42))
            self.assertEqual(runtime.chunk_payloads, [_CONTENT])
            self.assertEqual(result.path, add_file.runtime_path)
            placeholder = result.model_placeholder().text
            self.assertEqual(placeholder, f'<workspace_file path="{result.path}" />')
            for forbidden in (
                "telegram-capability-never-model-visible",
                "telegram-stable-identity-never-path",
                "../../not-a-host-path.sh",
                "application/x-shellscript",
                "workspace-only",
            ):
                self.assertNotIn(forbidden, placeholder)

        asyncio.run(scenario())

    def test_group_scope_is_the_whole_group_not_topic_or_sender(self) -> None:
        """@brief 群附件归属完整群而非 Topic 或发送者 / A group attachment belongs to the whole group, not a topic or sender.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行群聊附件导入 / Run a group attachment import.

            @return None / None.
            """

            runtime = _RuntimeProcess()
            command = _command(upload=_reference(), is_group=True)
            await CurrentTurnWorkspaceAttachmentPreprocessor(
                source=_UploadSource(),
                runtime_process=runtime,  # type: ignore[arg-type]
                intents=_IntentStore(),
                receipts=_ReceiptStore(),
                history_invalidator=_HistoryInvalidator(),
            ).preprocess(command)
            self.assertEqual(runtime.commands[0].scope, GroupRuntimeScope(-10042))

        asyncio.run(scenario())

    def test_import_request_is_stable_and_completed_retry_replays_before_download(
        self,
    ) -> None:
        """@brief 同一 Turn 重试先只读 replay，且首次 chunks 保持协议有界 / Retrying one Turn first performs read-only replay while first-attempt chunks remain protocol-bounded.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 重复导入一个跨 chunk 边界的 Document / Import a Document crossing chunk boundaries twice.

            @return None / None.
            """

            content = b"x" * (MAX_ADD_FILE_CHUNK_BYTES * 2 + 7)
            source = _UploadSource(content)
            runtime = _RuntimeProcess()
            command = _command(upload=_reference())
            preprocessor = CurrentTurnWorkspaceAttachmentPreprocessor(
                source=source,
                runtime_process=runtime,  # type: ignore[arg-type]
                intents=_IntentStore(),
                receipts=_ReceiptStore(),
                history_invalidator=_HistoryInvalidator(),
            )

            first = await preprocessor.preprocess(command)
            second = await preprocessor.preprocess(command)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(len(runtime.commands), 1)
            first_command = runtime.commands[0]
            self.assertEqual(len(runtime.replay_commands), 2)
            first_replay, second_replay = runtime.replay_commands
            self.assertEqual(first_command.opaque_id, first_replay.opaque_id)
            self.assertEqual(first_command.request_id, first_replay.request_id)
            self.assertEqual(first_replay.request_id, second_replay.request_id)
            self.assertEqual(first_replay.request_hash, second_replay.request_hash)
            self.assertEqual(
                runtime.chunk_lengths,
                [
                    (MAX_ADD_FILE_CHUNK_BYTES, MAX_ADD_FILE_CHUNK_BYTES, 7),
                ],
            )
            self.assertEqual(runtime.chunk_payloads, [content])
            self.assertEqual(source.references, [command.current_turn_upload])
            assert second is not None
            self.assertTrue(second.replayed)

        asyncio.run(scenario())

    def test_no_document_never_downloads_or_activates_runtime(self) -> None:
        """@brief 没有 Document 时不下载也不激活 runtime / Without a Document, neither download nor runtime activation occurs.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行无附件命令 / Run a command without an attachment.

            @return None / None.
            """

            source = _UploadSource()
            runtime = _RuntimeProcess()
            result = await CurrentTurnWorkspaceAttachmentPreprocessor(
                source=source,
                runtime_process=runtime,  # type: ignore[arg-type]
                intents=_IntentStore(),
                receipts=_ReceiptStore(),
                history_invalidator=_HistoryInvalidator(),
            ).preprocess(_command())
            self.assertIsNone(result)
            self.assertEqual(source.references, [])
            self.assertEqual(runtime.commands, [])

        asyncio.run(scenario())

    def test_native_success_receipt_failure_then_expired_source_retries_by_journal_replay(
        self,
    ) -> None:
        """@brief native 成功后 DB receipt 中断时，过期 Telegram source 不妨碍 retry publish / After native success and DB receipt interruption, an expired Telegram source does not prevent retry publication.

        @return None / None.
        @note 这是 recovery gap 的定向回归：第一次已写 native completed journal，却在 receipt
            前失败；第二次必须先 replay，不能重新下载，并最终让 receipt 成为唯一模型可见
            publish witness。/ This is the directed recovery-gap regression: the first attempt
            writes a native completed journal but fails before its receipt; the second must replay
            first, must not re-download, and finally makes the receipt the sole model-visible
            publish witness.
        """

        async def scenario() -> None:
            """@brief 执行 native-success → receipt-failure → expired-source → replay-publish 序列 / Execute native-success → receipt-failure → expired-source → replay-publish.

            @return None / None.
            """

            command = _command(upload=_reference())
            source = _ExpiringUploadSource()
            runtime = _RuntimeProcess()
            intents = _IntentStore()
            receipts = _ReceiptStore(
                WorkspaceAttachmentReceiptUnavailableError(
                    "test receipt transaction lost"
                ),
                failures_remaining=1,
            )
            preprocessor = CurrentTurnWorkspaceAttachmentPreprocessor(
                source=source,
                runtime_process=runtime,  # type: ignore[arg-type]
                intents=intents,
                receipts=receipts,
                history_invalidator=_HistoryInvalidator(),
            )

            with self.assertRaises(WorkspaceAttachmentImportPendingError) as pending:
                await preprocessor.preprocess(command)
            self.assertIsInstance(pending.exception, InferenceDependencyPending)
            self.assertEqual(pending.exception.retry_after, timedelta(seconds=5))
            self.assertIsInstance(
                pending.exception.__cause__,
                WorkspaceAttachmentReceiptUnavailableError,
            )
            self.assertEqual(len(intents.prepared), 1)
            self.assertEqual(len(runtime.commands), 1)
            self.assertEqual(len(runtime.replay_commands), 1)
            self.assertEqual(source.references, [command.current_turn_upload])
            self.assertEqual(receipts.published, [])

            recovered = await preprocessor.preprocess(command)

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertTrue(recovered.replayed)
            self.assertEqual(len(runtime.commands), 1)
            self.assertEqual(len(runtime.replay_commands), 2)
            self.assertEqual(source.references, [command.current_turn_upload])
            self.assertEqual(len(receipts.records), 2)
            self.assertEqual(len(receipts.published), 1)
            published_command, published_receipt = receipts.published[0]
            self.assertEqual(published_command, command)
            self.assertEqual(published_receipt.path, recovered.path)
            self.assertEqual(
                recovered.model_placeholder().text,
                f'<workspace_file path="{published_receipt.path}" />',
            )

        asyncio.run(scenario())

    def test_prepared_intent_runtime_failure_becomes_non_exhausting_dependency(
        self,
    ) -> None:
        """@brief 已提交 intent 的 runtime 故障成为不耗尽普通预算的依赖 / A runtime failure after committed intent becomes a dependency that does not exhaust the ordinary budget.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 两次恢复同一无 receipt intent / Recover the same receipt-less intent twice.

            @return None / None.
            """

            command = _command(upload=_reference())
            source = _ExpiringUploadSource()
            runtime = _FailingReplayRuntime(
                WorkspaceRuntimeUnavailableError("test runtime unavailable")
            )
            intents = _IntentStore()
            preprocessor = CurrentTurnWorkspaceAttachmentPreprocessor(
                source=source,
                runtime_process=runtime,
                intents=intents,
                receipts=_ReceiptStore(),
                history_invalidator=_HistoryInvalidator(),
            )

            for _attempt in range(2):
                with self.assertRaises(
                    WorkspaceAttachmentImportPendingError
                ) as pending:
                    await preprocessor.preprocess(command)
                self.assertIsInstance(pending.exception, InferenceDependencyPending)
                self.assertEqual(
                    pending.exception.retry_after,
                    timedelta(seconds=5),
                )
                self.assertIsInstance(
                    pending.exception.__cause__,
                    WorkspaceRuntimeUnavailableError,
                )
            self.assertEqual(len(intents.prepared), 1)
            self.assertEqual(source.references, [command.current_turn_upload])
            self.assertEqual(len(runtime.replay_commands), 2)
            self.assertEqual(runtime.commands, [])

        asyncio.run(scenario())

    def test_durable_adapter_preserves_prepared_import_dependency(self) -> None:
        """@brief durable adapter 不得把附件恢复依赖降级为永久失败 / The durable adapter must not downgrade attachment recovery dependency to permanent failure.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 从真实 pre-Agent 边界观察强类型 dependency / Observe the typed dependency at the real pre-Agent boundary.

            @return None / None.
            """

            command = _command(upload=_reference())
            source = _ExpiringUploadSource()
            runtime = _FailingReplayRuntime(
                WorkspaceRuntimeUnavailableError("test runtime unavailable")
            )
            adapter = DurableAssistantInferenceAdapter(
                history=_History(_anchor(command)),
                system_prompt="system prompt",
                runtime_limits=InferenceRuntimeLimits(
                    provider_timeout=timedelta(seconds=10),
                    attempt_timeout=timedelta(seconds=20),
                    lease_for=timedelta(seconds=30),
                ),
                inference=_Inference(),
                attachment_preprocessor=CurrentTurnWorkspaceAttachmentPreprocessor(
                    source=source,
                    runtime_process=runtime,
                    intents=_IntentStore(),
                    receipts=_ReceiptStore(),
                    history_invalidator=_HistoryInvalidator(),
                ),
            )

            with self.assertRaises(WorkspaceAttachmentImportPendingError) as pending:
                await adapter.infer(command.to_json())

            self.assertIsInstance(pending.exception, InferenceDependencyPending)
            self.assertEqual(pending.exception.retry_after, timedelta(seconds=5))
            self.assertIsInstance(
                pending.exception.__cause__,
                WorkspaceRuntimeUnavailableError,
            )
            self.assertEqual(source.references, [command.current_turn_upload])
            self.assertEqual(len(runtime.replay_commands), 1)

        asyncio.run(scenario())

    def test_post_receipt_cache_failure_is_not_an_unresolved_import(self) -> None:
        """@brief receipt 已提交后的 cache 故障保持普通重试语义 / A cache failure after committed receipt retains ordinary retry semantics.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 发布 receipt 后注入 history invalidation 故障 / Inject history invalidation failure after receipt publication.

            @return None / None.
            """

            command = _command(upload=_reference())
            receipts = _ReceiptStore()
            with self.assertRaises(
                WorkspaceAttachmentReceiptUnavailableError
            ) as unavailable:
                await CurrentTurnWorkspaceAttachmentPreprocessor(
                    source=_UploadSource(),
                    runtime_process=_RuntimeProcess(),
                    intents=_IntentStore(),
                    receipts=receipts,
                    history_invalidator=_HistoryInvalidator(
                        RuntimeError("test cache unavailable")
                    ),
                ).preprocess(command)

            self.assertNotIsInstance(
                unavailable.exception,
                WorkspaceAttachmentImportPendingError,
            )
            self.assertEqual(len(receipts.published), 1)
            self.assertIsInstance(unavailable.exception.__cause__, RuntimeError)

        asyncio.run(scenario())

    def test_native_receipt_drift_waits_for_prepared_intent_reconciliation(
        self,
    ) -> None:
        """@brief 已有 intent 时错误 native receipt 进入恢复依赖且不制造路径 / With an intent, a wrong native receipt enters recovery dependency and creates no path.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行不匹配收据场景 / Run the mismatched-receipt scenario.

            @return None / None.
            """

            with self.assertRaises(WorkspaceAttachmentImportPendingError) as pending:
                await CurrentTurnWorkspaceAttachmentPreprocessor(
                    source=_UploadSource(),
                    runtime_process=_RuntimeProcess(mismatched_receipt=True),  # type: ignore[arg-type]
                    intents=_IntentStore(),
                    receipts=_ReceiptStore(),
                    history_invalidator=_HistoryInvalidator(),
                ).preprocess(_command(upload=_reference()))
            self.assertIsInstance(
                pending.exception.__cause__,
                WorkspaceRuntimeProtocolError,
            )

        asyncio.run(scenario())

    def test_durable_adapter_classifies_attachment_failure_taxonomy(self) -> None:
        """@brief 网络重试、用户超限、provider 违约与未知错误具有不同终态 / Network retry, user oversize, provider-contract violations, and unknown errors have distinct terminal semantics.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 在真实 pre-Agent 入口验证所有分类 / Verify every category at the real pre-Agent boundary.

            @return None / None.
            """

            command = _command(upload=_reference())
            cases = (
                (
                    CurrentTurnUploadTransportError("network unavailable"),
                    RetryableInferenceError,
                    InferenceErrorCategory.NETWORK,
                ),
                (
                    CurrentTurnUploadTooLargeError("too large"),
                    PermanentInferenceError,
                    InferenceErrorCategory.INVALID_REQUEST,
                ),
                (
                    CurrentTurnUploadDownloadError("non-bytes provider response"),
                    PermanentInferenceError,
                    InferenceErrorCategory.PROVIDER,
                ),
                (
                    CurrentTurnUploadIntegrityError("unexpected provider identity"),
                    PermanentInferenceError,
                    InferenceErrorCategory.PROVIDER,
                ),
                (
                    RuntimeError("programming defect must not retry forever"),
                    PermanentInferenceError,
                    InferenceErrorCategory.INTERNAL,
                ),
            )
            for error, expected_type, expected_category in cases:
                with self.subTest(error=type(error).__name__):
                    runtime = _RuntimeProcess()
                    adapter = DurableAssistantInferenceAdapter(
                        history=_History(_anchor(command)),
                        system_prompt="system prompt",
                        runtime_limits=InferenceRuntimeLimits(
                            provider_timeout=timedelta(seconds=10),
                            attempt_timeout=timedelta(seconds=20),
                            lease_for=timedelta(seconds=30),
                        ),
                        inference=_Inference(),  # type: ignore[arg-type]
                        attachment_preprocessor=CurrentTurnWorkspaceAttachmentPreprocessor(
                            source=_FailingUploadSource(error),  # type: ignore[arg-type]
                            runtime_process=runtime,  # type: ignore[arg-type]
                            intents=_IntentStore(),
                            receipts=_ReceiptStore(),
                            history_invalidator=_HistoryInvalidator(),
                        ),
                    )
                    with self.assertRaises(expected_type) as captured:
                        await adapter.infer(command.to_json())
                    self.assertIs(captured.exception.category, expected_category)
                    self.assertEqual(runtime.commands, [])

        asyncio.run(scenario())

    def test_durable_adapter_replaces_current_message_before_agent_call(self) -> None:
        """@brief durable adapter 在 Agent 前以路径占位符替换当前消息 / The durable adapter replaces the current message with a path placeholder before the Agent.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行完整的 pre-Agent 替换路径 / Run the complete pre-Agent replacement path.

            @return None / None.
            """

            command = _command(upload=_reference())
            runtime = _RuntimeProcess()
            inference = _Inference()
            adapter = DurableAssistantInferenceAdapter(
                history=_History(_anchor(command)),
                system_prompt="system prompt",
                runtime_limits=InferenceRuntimeLimits(
                    provider_timeout=timedelta(seconds=10),
                    attempt_timeout=timedelta(seconds=20),
                    lease_for=timedelta(seconds=30),
                ),
                inference=inference,  # type: ignore[arg-type]
                attachment_preprocessor=CurrentTurnWorkspaceAttachmentPreprocessor(
                    source=_UploadSource(),
                    runtime_process=runtime,  # type: ignore[arg-type]
                    intents=_IntentStore(),
                    receipts=_ReceiptStore(),
                    history_invalidator=_HistoryInvalidator(),
                ),
            )

            await adapter.infer(command.to_json())

            self.assertIsNotNone(inference.context)
            assert inference.context is not None
            user_messages = [
                message
                for message in inference.context.messages
                if message.role is MessageRole.USER
            ]
            self.assertEqual(len(user_messages), 1)
            placeholder = user_messages[0].text
            self.assertEqual(
                placeholder,
                f'<workspace_file path="{runtime.commands[0].runtime_path}" />',
            )
            self.assertNotIn("caption that must not reach the model", placeholder)
            self.assertEqual(inference.context.current_user_text, placeholder)
            model_text = "\n".join(
                message.text for message in inference.context.messages
            )
            for forbidden in (
                "telegram-capability-never-model-visible",
                "telegram-stable-identity-never-path",
                "../../not-a-host-path.sh",
                "application/x-shellscript",
                "caption that must not reach the model",
            ):
                self.assertNotIn(forbidden, model_text)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
