"""@brief 无 Turn 出站副作用的应用能力 / Application capability for outbound effects without a Turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.outbox import OutboundKind


@dataclass(frozen=True, slots=True)
class StandaloneOutboundCommand:
    """@brief 幂等写入 standalone outbox 的通用命令 / Generic command idempotently writing the standalone outbox.

    @param conversation_id 副作用幂等作用域 / Effect-idempotency scope.
    @param delivery_stream_id 有序投递流 / Ordered delivery stream.
    @param kind connector 动作类型 / Connector action kind.
    @param payload connector-neutral JSON payload / Connector-neutral JSON payload.
    @param idempotency_key 来源事件派生幂等键 / Source-event-derived idempotency key.
    @param created_at 创建时间 / Creation time.
    """

    conversation_id: ConversationId
    delivery_stream_id: DeliveryStreamId
    kind: OutboundKind
    payload: JsonObject
    idempotency_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验命令并冻结可变 payload / Validate the command and isolate its mutable payload.

        @return None / None.
        """

        key = self.idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError(
                "Standalone outbound idempotency key must contain 1-255 characters"
            )
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


class StandaloneOutboundCapability(Protocol):
    """@brief 将无 Turn 副作用幂等写入 outbox 的能力 / Capability idempotently writing Turn-less effects to the outbox."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 幂等入队副作用 / Idempotently enqueue an effect.

        @param command 类型化出站命令 / Typed outbound command.
        @return None / None.
        """

        ...


__all__ = ["StandaloneOutboundCapability", "StandaloneOutboundCommand"]
