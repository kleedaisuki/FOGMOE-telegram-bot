"""@brief Assistant workspace 文件发送工具测试 / Assistant workspace-file delivery tool tests."""

import asyncio
import hashlib
from pathlib import Path
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import DEFAULT_TOOL_CATALOG
from fogmoe_bot.application.workspace.models import FetchFileCommand, FetchFileResult
from fogmoe_bot.application.workspace.ports import RuntimeProcess
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_ARTIFACT, OutboundDraft
from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.infrastructure.assistant.tool_operations.outbound import (
    finalize_downstream_effect,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.workspace import (
    execute_send_workspace_file,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore


class _Runtime:
    """@brief 返回固定 bytes 的 RuntimeProcess 替身 / RuntimeProcess double returning fixed bytes."""

    def __init__(self, content: bytes) -> None:
        """@brief 保存文件内容 / Store file content.

        @param content 待读取 bytes / Bytes to fetch.
        """

        self.content = content
        self.commands: list[FetchFileCommand] = []

    async def fetch_file(self, command: FetchFileCommand) -> FetchFileResult:
        """@brief 记录并返回绑定摘要的文件 / Record and return a digest-bound file.

        @param command 读取命令 / Fetch command.
        @return 完整文件 / Complete file.
        """

        self.commands.append(command)
        return FetchFileResult(
            path=command.path,
            content=self.content,
            sha256=hashlib.sha256(self.content).hexdigest(),
        )


class _Outbox:
    """@brief 记录 standalone outbox 草稿 / Record standalone-outbox drafts."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recording."""

        self.drafts: list[OutboundDraft] = []

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> None:
        """@brief 记录同事务下游意图 / Record the downstream intent in the same transaction.

        @param connection receipt finalize connection / Receipt-finalize connection.
        @param draft artifact outbox 草稿 / Artifact-outbox draft.
        """

        del connection
        self.drafts.append(draft)


def _request() -> ToolEffectRequest:
    """@brief 构造群 topic 文件发送请求 / Build a group-topic file-send request.

    @return 已认证工具请求 / Authenticated tool request.
    """

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-group:-100:thread:11"),
            delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-100:thread:11"),
            user_id=42,
            chat_id=-100,
            is_group=True,
            group_id=-100,
            message_id=7,
            message_thread_id=11,
        ),
        invocation_id="step:1:call:0",
        provider_call_id="provider-workspace-file",
        tool_name="send_workspace_file",
        effect_kind="telegram.send_workspace_file",
        mutating=True,
        arguments={"path": "reports/result.txt"},
        request_hash="a" * 64,
    )


def test_catalog_exposes_bounded_workspace_file_tool() -> None:
    """@brief catalog 只接受 workspace 相对路径 / Catalog accepts only workspace-relative paths."""

    accepted = DEFAULT_TOOL_CATALOG.validate(
        "send_workspace_file", {"path": "reports/result.txt"}
    )
    rejected = DEFAULT_TOOL_CATALOG.validate(
        "send_workspace_file", {"path": "../etc/passwd"}
    )
    assert accepted.mutating is True
    assert str(accepted.effect_kind) == "telegram.send_workspace_file"
    assert not hasattr(rejected, "mutating")


def test_workspace_file_is_persisted_before_outbox_and_topic_is_preserved(
    tmp_path: Path,
) -> None:
    """@brief 工具先 durable 存储，再由 finalize 写 topic-aware outbox / Tool durably stores before finalize writes a topic-aware outbox.

    @param tmp_path 临时 artifact store / Temporary artifact store.
    """

    async def scenario() -> None:
        """@brief 执行 operation/finalize 两阶段 / Run the operation/finalize phases."""

        request = _request()
        runtime = _Runtime(b"workspace-result")
        artifacts = FileArtifactStore(tmp_path)
        result = await execute_send_workspace_file(
            request,
            runtime_process=cast(RuntimeProcess, runtime),
            artifacts=artifacts,
            artifact_bulkhead=AsyncBlockingBulkhead(
                capacity=1,
                queue_timeout=1.0,
                call_timeout=5.0,
                task_name="test-workspace-file-artifact",
            ),
        )
        assert isinstance(result, dict)
        artifact_values = result["artifacts"]
        assert isinstance(artifact_values, list)
        artifact = artifact_values[0]
        assert isinstance(artifact, dict)
        assert runtime.commands[0].path.value == "reports/result.txt"
        assert artifact["kind"] == ArtifactKind.DOCUMENT.value
        outbox = _Outbox()
        await finalize_downstream_effect(
            request,
            result,
            connection=cast(AsyncConnection, object()),
            outbox=cast(StandaloneOutboxWriter, outbox),
        )
        assert len(outbox.drafts) == 1
        draft = outbox.drafts[0]
        assert draft.kind == SEND_TELEGRAM_ARTIFACT
        assert draft.payload == {
            "chat_id": -100,
            "artifact_id": artifact["artifact_id"],
            "kind": "document",
            "filename": "result.txt",
            "mime_type": "text/plain",
            "size_bytes": len(b"workspace-result"),
            "message_thread_id": 11,
        }

    asyncio.run(scenario())
