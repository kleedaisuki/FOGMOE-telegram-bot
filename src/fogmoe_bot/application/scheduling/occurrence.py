"""@brief 将 schedule occurrence 纯构造成 Conversation acceptance / Pure construction of Conversation acceptance from a schedule occurrence."""

from __future__ import annotations

from datetime import datetime

from fogmoe_bot.application.assistant.inference_command import (
    ASSISTANT_INFERENCE_SCHEMA_VERSION,
    DurableAssistantInferenceCommand,
    DurableAssistantScope,
    DurableAssistantUser,
)
from fogmoe_bot.application.conversation.workflow import (
    AcceptConversationTurn,
    ConversationWorkflow,
    PreparedTurnAcceptance,
)
from fogmoe_bot.domain.context import ScheduledTaskContext, render_scheduled_task
from fogmoe_bot.domain.conversation.identity import TurnId, TurnSource
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.scheduling.assistant_schedule import ScheduledAssistantTurn
from fogmoe_bot.domain.temporal import ensure_utc


SCHEDULED_PROMPT_SOURCE_KIND = "schedule.prompt"
"""@brief Schedule occurrence 的 TurnSource namespace / TurnSource namespace for schedule occurrences."""

SCHEDULED_READ_TOOL_NAMES = (
    "get_current_time",
    "google_search",
    "fetch_url",
    "search_memory",
    "search_memory_by_time",
    "list_available_stickers",
)
"""@brief Scheduled Turn 可用的只读工具 capability / Read-only tool capability available to scheduled Turns."""


def prepare_scheduled_occurrence(
    schedule: ScheduledAssistantTurn,
    *,
    user: DurableAssistantUser,
    observed_at: datetime,
) -> PreparedTurnAcceptance:
    """@brief 纯构造确定性 scheduled Turn acceptance / Purely construct a deterministic scheduled-Turn acceptance.

    @param schedule 当前持久化 occurrence / Current persisted occurrence.
    @param user acceptance-time 创建者快照 / Creator snapshot at acceptance time.
    @param observed_at worker 观察时刻 / Worker observation instant.
    @return 可交给跨聚合 UoW 的 prepared acceptance / Prepared acceptance for the cross-aggregate UoW.
    @note Scheduled Turn 仅授予显式只读 allowlist；Prompt 文本不是授权边界。/
        Scheduled Turns receive only an explicit read-only allowlist; prompt text is not an authorization boundary.
    """

    if user.user_id != schedule.creator_user_id:
        raise ValueError("Scheduled user snapshot does not match the schedule creator")
    observed = ensure_utc(observed_at)
    scheduled_for = schedule.next_run_at
    target = schedule.target
    safe_user = (
        user.model_copy(
            update={"profile": None, "personal_info": "", "diary_exists": False}
        )
        if target.is_group
        else user
    )
    source = TurnSource.external(
        SCHEDULED_PROMPT_SOURCE_KIND,
        occurrence_key(schedule.schedule_id, scheduled_for),
    )
    turn_id = TurnId.for_source(source)
    scheduled_text = render_scheduled_task(
        ScheduledTaskContext(
            timestamp=observed,
            scheduled_at=schedule.created_at,
            scheduled_for=scheduled_for,
            trigger_reason=schedule.trigger_reason,
            context_text=schedule.context_snapshot,
            instruction=schedule.instruction,
        )
    )
    user_content: JsonObject = {
        "text": scheduled_text,
        "content_kind": "scheduled_prompt",
        "source": {
            "kind": SCHEDULED_PROMPT_SOURCE_KIND,
            "schedule_id": schedule.schedule_id,
            "scheduled_for": _instant_text(scheduled_for),
        },
    }
    inference_request = DurableAssistantInferenceCommand(
        schema_version=ASSISTANT_INFERENCE_SCHEMA_VERSION,
        conversation_id=str(target.conversation_id),
        turn_id=str(turn_id),
        delivery_stream_id=str(target.delivery_stream_id),
        chat_id=target.chat_id,
        reply_to_message_id=None,
        message_thread_id=target.message_thread_id,
        user=safe_user,
        scope=DurableAssistantScope(
            is_group=target.is_group,
            group_id=target.group_id,
            message_id=None,
            message_thread_id=target.message_thread_id,
        ),
        disable_notification=False,
        protect_content=False,
        disable_web_page_preview=False,
        allow_tools=True,
        allowed_tools=SCHEDULED_READ_TOOL_NAMES,
    ).to_json()
    return ConversationWorkflow.prepare(
        AcceptConversationTurn(
            source=source,
            conversation_id=target.conversation_id,
            user_content=user_content,
            inference_request=inference_request,
            received_at=observed,
            accepted_at=observed,
            trace_context=TraceContext.new_root(),
        )
    )


def occurrence_key(schedule_id: int, scheduled_for: datetime) -> str:
    """@brief 构造稳定 occurrence identity / Build a stable occurrence identity.

    @param schedule_id 永不复用的 schedule ID / Never-reused schedule identifier.
    @param scheduled_for 当前 planned UTC instant / Current planned UTC instant.
    @return ``schedule_id:RFC3339`` / ``schedule_id:RFC3339``.
    """

    if isinstance(schedule_id, bool) or schedule_id <= 0:
        raise ValueError("schedule_id must be positive")
    return f"{schedule_id}:{_instant_text(scheduled_for, timespec='microseconds')}"


def _instant_text(value: datetime, *, timespec: str = "seconds") -> str:
    """@brief 序列化 UTC occurrence / Serialize a UTC occurrence.

    @param value aware 时间 / Aware datetime.
    @param timespec ISO 精度 / ISO precision.
    @return Z 结尾文本 / Z-suffixed text.
    """

    return ensure_utc(value).isoformat(timespec=timespec).replace("+00:00", "Z")


__all__ = [
    "SCHEDULED_PROMPT_SOURCE_KIND",
    "SCHEDULED_READ_TOOL_NAMES",
    "occurrence_key",
    "prepare_scheduled_occurrence",
]
