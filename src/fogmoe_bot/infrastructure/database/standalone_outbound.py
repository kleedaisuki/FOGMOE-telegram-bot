"""@brief PostgreSQL standalone outbox 适配器 / PostgreSQL standalone-outbox adapter."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.domain.conversation.outbox import OutboundDraft
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)


class StandaloneOutboundRepository(Protocol):
    """@brief adapter 所需的最窄 outbox 端口 / Narrow outbox port required by the adapter."""

    async def enqueue_standalone_outbound(
        self,
        draft: OutboundDraft,
    ) -> object:
        """@brief 幂等写入 standalone outbox / Idempotently enqueue a standalone outbox effect.

        @param draft 无 Turn 的出站草稿 / Outbound draft without a Turn.
        @return 仓储回执；adapter 不解释其内容 / Repository receipt, opaque to the adapter.
        """

        ...


class PostgresStandaloneOutboundCapability:
    """@brief 将通用无 Turn 副作用持久化到 outbox / Persist generic Turn-less effects to the outbox."""

    def __init__(
        self,
        repository: StandaloneOutboundRepository | None = None,
    ) -> None:
        """@brief 注入 standalone outbox 仓储 / Inject the standalone-outbox repository.

        @param repository 可选仓储替身 / Optional repository substitute.
        """

        self._repository = (
            repository if repository is not None else PostgresOutboxRepository()
        )

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 以确定性 ID 幂等写入副作用 / Idempotently persist an effect with a deterministic ID.

        @param command 类型化出站命令 / Typed outbound command.
        @return None / None.
        @note 事务边界由仓储拥有；本 adapter 不暴露 AsyncConnection / The repository owns
            the transaction boundary; this adapter does not expose AsyncConnection.
        """

        draft = OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                command.conversation_id,
                command.idempotency_key,
            ),
            conversation_id=command.conversation_id,
            turn_id=None,
            delivery_stream_id=command.delivery_stream_id,
            kind=command.kind,
            payload=command.payload,
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
        )
        await self._repository.enqueue_standalone_outbound(draft)


__all__ = [
    "PostgresStandaloneOutboundCapability",
    "StandaloneOutboundRepository",
]
