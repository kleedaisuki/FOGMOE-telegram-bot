"""Assistant tool operation-mode dispatcher / Assistant 工具 operation-mode 分派器."""

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_STICKER
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    ToolTransactionMode,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)

from .diary import execute_diary
from .external import ExternalReadTools, GeneratedMediaTools, StickerCatalogReader
from .group import GroupContextReader, fetch_group_context
from .memory import (
    PermanentMemoryReader,
    fetch_permanent_summaries,
    search_permanent_records,
)
from .outbound import finalize_downstream_effect
from .parsing import optional_text, required_connection, required_text
from .schedule import execute_schedule
from .social import execute_impression_update, execute_kindness_gift


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
        memory: PermanentMemoryReader,
        groups: GroupContextReader,
    ) -> None:
        """注入全部显式 adapter；工具 metadata 仍仅由 ToolCatalog 拥有。"""

        self._help_text = help_text
        self._external_reads = external_reads
        self._generated_media = generated_media
        self._stickers = stickers
        self._outbox = outbox
        self._memory = memory
        self._groups = groups

    def transaction_mode(self, request: ToolEffectRequest) -> ToolTransactionMode:
        """按 catalog 提供的 mutation/effect classification 选择事务模式。"""

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
            case "google_search" | "fetch_url" | "execute_python_code":
                return await self._external_reads.execute(request)
            case "fetch_group_context":
                return await fetch_group_context(request, groups=self._groups)
            case "fetch_permanent_summaries":
                return await fetch_permanent_summaries(request, memory=self._memory)
            case "search_permanent_records":
                return await search_permanent_records(request, memory=self._memory)
            case "user_diary":
                return await execute_diary(request, connection=connection)
            case "schedule_ai_message":
                return await execute_schedule(request, connection=connection)
            case "kindness_gift":
                return await execute_kindness_gift(
                    request,
                    connection=required_connection(connection),
                )
            case "update_impression":
                return await execute_impression_update(
                    request,
                    connection=required_connection(connection),
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
