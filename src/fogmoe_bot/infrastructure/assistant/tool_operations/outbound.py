"""Assistant tool downstream outbox finalization / Assistant 工具下游 outbox 终结。"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_ARTIFACT,
    SEND_TELEGRAM_STICKER,
    OutboundDraft,
)
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    StandaloneOutboxWriter,
)

from .parsing import bounded_int, required_text


async def finalize_downstream_effect(
    request: ToolEffectRequest,
    result: JsonValue,
    *,
    connection: AsyncConnection,
    outbox: StandaloneOutboxWriter,
) -> None:
    """在 succeeded receipt 同一短事务写入 durable downstream intent。"""

    if request.effect_kind == SEND_TELEGRAM_STICKER.value:
        await _enqueue_sticker(request, connection=connection, outbox=outbox)
        return
    if not request.effect_kind.startswith("media.") or not isinstance(result, dict):
        return
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return
    now = datetime.now(UTC)
    for index, artifact in enumerate(artifacts[:10]):
        if not isinstance(artifact, dict):
            continue
        payload: JsonObject = {
            "chat_id": request.context.chat_id,
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "kind": str(artifact.get("kind") or ""),
            "filename": str(artifact.get("filename") or "artifact.bin"),
            "mime_type": str(artifact.get("mime_type") or "application/octet-stream"),
            "size_bytes": bounded_int(artifact, "size_bytes", minimum=1),
        }
        idempotency_key = (
            f"assistant-tool:{request.context.turn_id}:"
            f"{request.invocation_id}:artifact:{index}"
        )
        await outbox.enqueue_standalone_outbound_in_transaction(
            connection,
            OutboundDraft(
                message_id=OutboundMessageId.for_conversation(
                    request.context.conversation_id,
                    idempotency_key,
                ),
                conversation_id=request.context.conversation_id,
                turn_id=None,
                delivery_stream_id=request.context.delivery_stream_id,
                kind=SEND_TELEGRAM_ARTIFACT,
                payload=payload,
                idempotency_key=idempotency_key,
                created_at=now,
            ),
        )


async def _enqueue_sticker(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection,
    outbox: StandaloneOutboxWriter,
) -> None:
    """将 pack/emoji intent 写入 standalone outbox，不持久化 file_id。"""

    payload: JsonObject = {
        "chat_id": request.context.chat_id,
        "pack_name": required_text(request.arguments, "pack_name"),
        "emoji": required_text(request.arguments, "emoji"),
    }
    if request.context.message_thread_id is not None:
        payload["message_thread_id"] = request.context.message_thread_id
    idempotency_key = (
        f"assistant-tool:{request.context.turn_id}:{request.invocation_id}:sticker"
    )
    await outbox.enqueue_standalone_outbound_in_transaction(
        connection,
        OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                request.context.conversation_id,
                idempotency_key,
            ),
            conversation_id=request.context.conversation_id,
            turn_id=None,
            delivery_stream_id=request.context.delivery_stream_id,
            kind=SEND_TELEGRAM_STICKER,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=datetime.now(UTC),
        ),
    )
