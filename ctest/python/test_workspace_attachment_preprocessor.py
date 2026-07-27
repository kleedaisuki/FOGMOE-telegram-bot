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
    DownloadedCurrentTurnUpload,
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
)
from fogmoe_bot.application.context_window.projection import (
    ContextWindowBounds,
    ContextWindowReady,
    ContextWindowRequest,
    ContextWindowResult,
)
from fogmoe_bot.application.conversation.inference_worker import (
    InferenceErrorCategory,
    InferenceRuntimeLimits,
    PermanentInferenceError,
    RetryableInferenceError,
)
from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeProtocolError
from fogmoe_bot.application.workspace.models import (
    MAX_ADD_FILE_CHUNK_BYTES,
    AddFileCommand,
    AddFileResult,
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


class _RuntimeProcess:
    """@brief 记录 add_file command 的 RuntimeProcess 替身 / RuntimeProcess double recording add_file commands."""

    def __init__(self, *, mismatched_receipt: bool = False) -> None:
        """@brief 配置收据是否故意漂移 / Configure whether the receipt deliberately drifts.

        @param mismatched_receipt 为 True 时返回错误 path / When True, return a wrong path.
        @return None / None.
        """

        self.mismatched_receipt = mismatched_receipt
        self.commands: list[AddFileCommand] = []
        self.chunk_payloads: list[bytes] = []
        self.chunk_lengths: list[tuple[int, ...]] = []

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
        return AddFileResult(
            request_id=command.request_id,
            replayed=len(self.commands) > 1,
            path=path,
            byte_size=command.byte_size,
            sha256=command.sha256,
        )


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
    content: JsonObject = {
        "text": original_text,
        "model_message": text_message(MessageRole.USER, original_text).to_json(),
    }
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
            ).preprocess(command)
            self.assertEqual(runtime.commands[0].scope, GroupRuntimeScope(-10042))

        asyncio.run(scenario())

    def test_import_request_is_stable_and_chunks_are_protocol_bounded(self) -> None:
        """@brief 同一 Turn 重试使用同一请求身份且按协议切块 / Retrying one Turn uses one request identity and protocol-bounded chunks.

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
            )

            first = await preprocessor.preprocess(command)
            second = await preprocessor.preprocess(command)

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(len(runtime.commands), 2)
            first_command, second_command = runtime.commands
            self.assertEqual(first_command.opaque_id, second_command.opaque_id)
            self.assertEqual(first_command.request_id, second_command.request_id)
            self.assertEqual(first_command.request_hash, second_command.request_hash)
            self.assertEqual(
                runtime.chunk_lengths,
                [
                    (MAX_ADD_FILE_CHUNK_BYTES, MAX_ADD_FILE_CHUNK_BYTES, 7),
                    (MAX_ADD_FILE_CHUNK_BYTES, MAX_ADD_FILE_CHUNK_BYTES, 7),
                ],
            )
            self.assertEqual(runtime.chunk_payloads, [content, content])
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
            ).preprocess(_command())
            self.assertIsNone(result)
            self.assertEqual(source.references, [])
            self.assertEqual(runtime.commands, [])

        asyncio.run(scenario())

    def test_native_receipt_drift_is_rejected(self) -> None:
        """@brief native 返回错误目标路径时不能制造模型占位符 / A native wrong target path cannot create a model placeholder.

        @return None / None.
        """

        async def scenario() -> None:
            """@brief 运行不匹配收据场景 / Run the mismatched-receipt scenario.

            @return None / None.
            """

            with self.assertRaises(WorkspaceRuntimeProtocolError):
                await CurrentTurnWorkspaceAttachmentPreprocessor(
                    source=_UploadSource(),
                    runtime_process=_RuntimeProcess(mismatched_receipt=True),  # type: ignore[arg-type]
                ).preprocess(_command(upload=_reference()))

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
