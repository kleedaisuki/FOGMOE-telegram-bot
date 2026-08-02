"""Assistant tool operation-mode dispatcher / Assistant 工具 operation-mode 分派器."""

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.temporal_memory import TemporalMemoryReader
from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.chat.ports import GroupContextReader
from fogmoe_bot.application.memory.ports import WorkingMemoryReader
from fogmoe_bot.application.scheduling.service import SchedulingService
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeUnavailableError
from fogmoe_bot.application.workspace.models import (
    AddFileCommand,
    AddFileResult,
    DEFAULT_BASH_OUTPUT_LIMIT_BYTES,
    FetchFileCommand,
    FetchFileResult,
    ReplayFileCommand,
    RunBashCommand,
    RunBashResult,
)
from fogmoe_bot.application.workspace.ports import RuntimeProcess
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_STICKER
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    ToolTransactionMode,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore

from .diary import execute_diary
from .external import ExternalReadTools, GeneratedMediaTools, StickerCatalogReader
from .group import fetch_group_context
from .memory import search_memory
from .outbound import finalize_downstream_effect
from .parsing import optional_text, required_connection, required_text
from .schedule import execute_schedule
from .temporal_memory import search_memory_by_time
from .time import get_current_time
from .workspace import execute_run_bash, execute_send_workspace_file


class _UnavailableRuntimeProcess:
    """@brief 未经组合根装配时的 fail-closed RuntimeProcess / Fail-closed RuntimeProcess used before composition-root wiring.

    @note 它不是本机 subprocess fallback；仅使不涉及 Workspace 的 operation 单测保持
        局部，同时确保任何 ``run_bash`` 调用明确失败。/ It is not a local-subprocess
        fallback; it only keeps unit tests for unrelated operations local while ensuring every
        ``run_bash`` invocation fails explicitly.
    """

    async def run_bash(self, command: RunBashCommand) -> RunBashResult:
        """@brief 拒绝未装配的 Bash 请求 / Reject an unwired Bash request.

        @param command 被拒绝的应用命令 / Rejected application command.
        @return 此函数永不返回 / This function never returns.
        @raise WorkspaceRuntimeUnavailableError 始终抛出，防止宿主机回退 /
            Always raised to prevent a host fallback.
        """

        del command
        raise WorkspaceRuntimeUnavailableError(
            "Workspace runtime is not configured in this process"
        )

    async def add_file(self, command: AddFileCommand) -> AddFileResult:
        """@brief 拒绝未装配的文件导入 / Reject an unwired file import.

        @param command 被拒绝的 add_file 应用命令 / Rejected add_file application command.
        @return 此函数永不返回 / This function never returns.
        @raise WorkspaceRuntimeUnavailableError 始终抛出，防止绕过 RuntimeProcess /
            Always raised to prevent bypassing RuntimeProcess.
        """

        del command
        raise WorkspaceRuntimeUnavailableError(
            "Workspace runtime is not configured in this process"
        )

    async def replay_file(self, command: ReplayFileCommand) -> AddFileResult:
        """@brief 拒绝未装配的附件 journal 重放 / Reject an unwired attachment-journal replay.

        @param command 被拒绝的 replay_file 应用命令 / Rejected replay_file application command.
        @return 此函数永不返回 / This function never returns.
        @raise WorkspaceRuntimeUnavailableError 始终抛出，防止绕过 RuntimeProcess /
            Always raised to prevent bypassing RuntimeProcess.
        """

        del command
        raise WorkspaceRuntimeUnavailableError(
            "Workspace runtime is not configured in this process"
        )

    async def fetch_file(self, command: FetchFileCommand) -> FetchFileResult:
        """@brief 拒绝未装配的 workspace 文件读取 / Reject an unwired workspace file fetch.

        @param command 被拒绝的读取命令 / Rejected fetch command.
        @return 此函数永不返回 / This function never returns.
        @raise WorkspaceRuntimeUnavailableError 始终抛出 / Always raised.
        """

        del command
        raise WorkspaceRuntimeUnavailableError(
            "Workspace runtime is not configured in this process"
        )


class AssistantToolOperationDispatcher:
    """将 catalog-validated requests 分派至内聚 feature operations。"""

    def __init__(
        self,
        *,
        help_text: str,
        external_reads: ExternalReadTools,
        generated_media: GeneratedMediaTools,
        stickers: StickerCatalogReader,
        outbox: StandaloneOutboxWriter,
        memory: WorkingMemoryReader,
        temporal_memory: TemporalMemoryReader,
        groups: GroupContextReader,
        time: TimeService,
        scheduling: SchedulingService,
        runtime_process: RuntimeProcess | None = None,
        artifacts: FileArtifactStore | None = None,
        workspace_file_bulkhead: AsyncBlockingBulkhead | None = None,
        workspace_output_limit_bytes: int = DEFAULT_BASH_OUTPUT_LIMIT_BYTES,
    ) -> None:
        """@brief 注入全部显式 adapter / Inject all explicit adapters.

        @param help_text 静态帮助文本 / Static help text.
        @param external_reads 只读外部工具 adapter / Read-only external-tools adapter.
        @param generated_media 生成媒体 adapter / Generated-media adapter.
        @param stickers 贴纸目录读取端口 / Sticker-catalog reader.
        @param outbox 独立 outbox 写端口 / Standalone outbox writer.
        @param memory Working Memory 读取端口 / Working-Memory reader.
        @param temporal_memory 时间检索端口 / Temporal-memory reader.
        @param groups 群上下文读取端口 / Group-context reader.
        @param time 时钟服务 / Time service.
        @param scheduling 日程服务 / Scheduling service.
        @param runtime_process fail-closed RuntimeProcess 端口；仅测试无关工具时可省略 /
            Fail-closed RuntimeProcess port; may be omitted only for unrelated-tool tests.
        @param artifacts durable artifact store；仅测试无关工具时可省略 /
            Durable artifact store; may be omitted only for unrelated-tool tests.
        @param workspace_file_bulkhead workspace artifact I/O 隔舱；仅测试无关工具时可省略 /
            Workspace-artifact I/O bulkhead; may be omitted only for unrelated-tool tests.
        @param workspace_output_limit_bytes ``run_bash`` 合并输出预算 /
            Combined output budget for ``run_bash``.
        @return None / None.
        @note 工具 metadata 仍仅由 ToolCatalog 拥有。/ Tool metadata remains owned solely
            by ToolCatalog.
        """

        self._help_text = help_text
        self._external_reads = external_reads
        self._generated_media = generated_media
        self._stickers = stickers
        self._outbox = outbox
        self._memory = memory
        self._temporal_memory = temporal_memory
        self._groups = groups
        self._time = time
        self._scheduling = scheduling
        self._runtime_process = runtime_process or _UnavailableRuntimeProcess()
        self._artifacts = artifacts
        self._workspace_file_bulkhead = workspace_file_bulkhead
        self._workspace_output_limit_bytes = workspace_output_limit_bytes

    def transaction_mode(self, request: ToolEffectRequest) -> ToolTransactionMode:
        """@brief 按 catalog 分类选择事务模式 / Select transaction mode from catalog classification.

        @param request 已校验工具请求 / Validated tool request.
        @return operation 所需的事务模式 / Transaction mode required by the operation.
        @note ``run_bash`` 的目的端 command journal 才是跨 DB crash gap 的幂等边界，
            RPC、stdout 管道与 timeout 绝不能留在数据库事务中。/ The destination command
            journal is ``run_bash``'s idempotency boundary across the DB crash gap; its RPC,
            stdout pipes, and timeout must never stay in a database transaction.
        """

        if request.tool_name in {"run_bash", "send_workspace_file"}:
            return ToolTransactionMode.OUTSIDE_TRANSACTION
        if request.mutating and not request.effect_kind.startswith("media."):
            return ToolTransactionMode.ATOMIC_MUTATION
        return ToolTransactionMode.OUTSIDE_TRANSACTION

    async def execute(
        self,
        request: ToolEffectRequest,
        *,
        connection: AsyncConnection | None,
    ) -> JsonValue:
        """分派一个已由权威 catalog 校验的 typed request。"""

        match request.tool_name:
            case "get_help_text":
                return {"help_text": self._help_text}
            case "get_current_time":
                return get_current_time(request, time=self._time)
            case "list_available_stickers":
                return await self._stickers.list_packs(
                    optional_text(request.arguments, "pack_name")
                )
            case "send_sticker":
                required_connection(connection)
                if (
                    request.effect_kind != SEND_TELEGRAM_STICKER.value
                    or not request.mutating
                ):
                    raise ValueError("send_sticker requires its mutating effect kind")
                return {
                    "status": "queued",
                    "pack_name": required_text(request.arguments, "pack_name"),
                    "emoji": required_text(request.arguments, "emoji"),
                }
            case "google_search" | "fetch_url":
                return await self._external_reads.execute(request)
            case "run_bash":
                return await execute_run_bash(
                    request,
                    runtime_process=self._runtime_process,
                    output_limit_bytes=self._workspace_output_limit_bytes,
                )
            case "send_workspace_file":
                if self._artifacts is None or self._workspace_file_bulkhead is None:
                    raise WorkspaceRuntimeUnavailableError(
                        "Workspace file delivery is not configured in this process"
                    )
                return await execute_send_workspace_file(
                    request,
                    runtime_process=self._runtime_process,
                    artifacts=self._artifacts,
                    artifact_bulkhead=self._workspace_file_bulkhead,
                )
            case "fetch_group_context":
                return await fetch_group_context(request, groups=self._groups)
            case "search_memory":
                return await search_memory(request, memory=self._memory)
            case "search_memory_by_time":
                return await search_memory_by_time(
                    request,
                    memory=self._temporal_memory,
                    time=self._time,
                )
            case "user_diary":
                return await execute_diary(request, connection=connection)
            case "schedule_ai_message":
                return await execute_schedule(
                    request,
                    connection=connection,
                    scheduling=self._scheduling,
                    time=self._time,
                )
            case "generate_image" | "generate_voice":
                return await self._generated_media.generate(request)
            case _:
                return {
                    "error": f"Tool operation is not configured: {request.tool_name}"
                }

    async def finalize(
        self,
        request: ToolEffectRequest,
        result: JsonValue,
        *,
        connection: AsyncConnection,
    ) -> None:
        """在 receipt finalize transaction 中持久化 downstream intent。"""

        await finalize_downstream_effect(
            request,
            result,
            connection=connection,
            outbox=self._outbox,
        )
