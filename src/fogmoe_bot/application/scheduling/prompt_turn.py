"""@brief 将调度发生项接受为 durable Conversation Turn / Accept schedule occurrences as durable Conversation Turns."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fogmoe_bot.application.assistant.inference_command import (
    ASSISTANT_INFERENCE_SCHEMA_VERSION,
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
)
from fogmoe_bot.application.conversation.workflow import (
    AcceptConversationTurn,
    ConversationWorkflow,
)
from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.domain.context import ScheduledTaskContext, render_scheduled_task
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
    TurnSource,
)
from fogmoe_bot.domain.scheduling import (
    PROMPT_JOB_KIND,
    JobKind,
    PromptJobPayload,
    ScheduledJob,
    ensure_utc,
)
from fogmoe_bot.domain.observability.trace import TraceContext


_PROMPT_SOURCE_KIND = "schedule.prompt"
"""@brief 调度 Prompt 的 TurnSource 命名空间 / TurnSource namespace for scheduled prompts."""


class ScheduledAssistantProfileReader(Protocol):
    """@brief 读取定时回合所需用户快照的端口 / Port reading the user snapshot required by a scheduled turn."""

    async def read(self, user_id: int) -> DurableAssistantUser | None:
        """@brief 读取 acceptance 时用户快照 / Read the user snapshot at acceptance time.

        @param user_id 调度所有者 ID / Schedule-owner identifier.
        @return 严格用户快照；用户不存在时为 None / Strict user snapshot, or None when absent.
        """

        ...


class PromptTurnHandler:
    """@brief 把每个 Prompt 调度发生项幂等写入 Conversation 工作流 / Idempotently write each prompt occurrence into the Conversation workflow.

    @note 该处理器不持有 Telegram Bot，不调用模型，也不发送消息；调度 lease 只覆盖
        一个短 acceptance。/ This handler owns no Telegram Bot, invokes no model, and sends no
        message; the schedule lease covers only a short acceptance.
    """

    def __init__(
        self,
        *,
        workflow: ConversationWorkflow,
        profiles: ScheduledAssistantProfileReader,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 注入 Conversation 工作流、用户快照端口与时钟 / Inject the Conversation workflow, profile port, and clock.

        @param workflow 统一 durable acceptance 工作流 / Unified durable-acceptance workflow.
        @param profiles 定时用户快照读取端口 / Scheduled-user snapshot reader.
        @param clock UTC 时钟 / UTC clock.
        """

        self._workflow = workflow
        """@brief 统一 Conversation acceptance / Unified Conversation acceptance."""
        self._profiles = profiles
        self._clock = clock or SystemUtcClock()

    @property
    def kind(self) -> JobKind:
        """@brief 返回处理器支持的任务类型 / Return the supported job kind.

        @return Assistant 定时回合类型 / Scheduled Assistant-turn kind.
        """

        return PROMPT_JOB_KIND

    async def handle(self, job: ScheduledJob[PromptJobPayload]) -> None:
        """@brief 接受一个已领取的调度发生项 / Accept one claimed schedule occurrence.

        @param job 已领取任务 / Claimed scheduled job.
        @return None / None.
        @raise LookupError 调度所有者已不存在 / The schedule owner no longer exists.
        """
        profile = await self._profiles.read(job.owner_id)
        if profile is None:
            raise LookupError(f"Scheduled job owner not found: {job.owner_id}")

        observed_at = self._clock.now()
        scheduled_for = ensure_utc(job.run_at)
        source = TurnSource.external(
            _PROMPT_SOURCE_KIND,
            _occurrence_key(job.schedule_id, scheduled_for),
        )
        turn_id = TurnId.for_source(source)
        conversation_id = ConversationId(f"assistant-user:{job.owner_id}")
        delivery_stream_id = DeliveryStreamId(
            f"telegram:primary:chat:{job.owner_id}:thread:0"
        )
        scheduled_text = render_scheduled_task(
            ScheduledTaskContext(
                timestamp=observed_at,
                scheduled_at=job.created_at,
                scheduled_for=scheduled_for,
                trigger_reason=job.payload.trigger_reason,
                context_text=job.payload.context_text,
                instruction=job.payload.instruction,
            )
        )
        user_content: JsonObject = {
            "text": scheduled_text,
            "content_kind": "scheduled_prompt",
            "source": {
                "kind": _PROMPT_SOURCE_KIND,
                "schedule_id": job.schedule_id,
                "scheduled_for": scheduled_for.isoformat(),
            },
        }
        inference_request = DurableAssistantInferenceCommand(
            schema_version=ASSISTANT_INFERENCE_SCHEMA_VERSION,
            conversation_id=str(conversation_id),
            turn_id=str(turn_id),
            delivery_stream_id=str(delivery_stream_id),
            chat_id=job.owner_id,
            reply_to_message_id=None,
            message_thread_id=None,
            user=profile,
            scope=DurableAssistantScope(
                is_group=False,
                group_id=None,
                message_id=None,
            ),
            disable_notification=False,
            protect_content=False,
            disable_web_page_preview=False,
        ).to_json()
        await self._workflow.accept(
            AcceptConversationTurn(
                source=source,
                conversation_id=conversation_id,
                user_content=user_content,
                inference_request=inference_request,
                received_at=observed_at,
                accepted_at=observed_at,
                trace_context=TraceContext.new_root(),
            )
        )


def _occurrence_key(schedule_id: int, scheduled_for: datetime) -> str:
    """@brief 构造不受本地时区表示影响的发生项键 / Build an occurrence key independent of local timezone representation.

    @param schedule_id 持久化调度 ID / Persisted schedule identifier.
    @param scheduled_for 已规范为 UTC 的 datetime / Datetime already normalized to UTC.
    @return ``schedule_id:UTC timestamp`` / ``schedule_id:UTC timestamp``.
    """

    canonical = (
        ensure_utc(scheduled_for)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return f"{schedule_id}:{canonical}"


__all__ = [
    "PromptTurnHandler",
    "ScheduledAssistantProfileReader",
]
