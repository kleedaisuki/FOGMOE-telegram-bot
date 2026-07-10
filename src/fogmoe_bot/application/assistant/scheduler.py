import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram.ext import ContextTypes

from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import ai_schedule_repository
from fogmoe_bot.application.telegram.archive_utils import send_permanent_records_archive
from fogmoe_bot.application.accounts.context import load_user_state
from fogmoe_bot.domain.context import (
    ConversationScope,
    ScheduledTaskContext,
)
from fogmoe_bot.infrastructure.telegram.telegram_utils import partial_send
from fogmoe_bot.application.assistant.tasks import summary
from fogmoe_bot.application.conversation_lock_manager import CONVERSATION_LOCK_MANAGER
from fogmoe_bot.application.telegram.generated_audio_sender import send_generated_audio_from_tool_logs
from fogmoe_bot.application.telegram.generated_image_sender import send_generated_images_from_tool_logs
from fogmoe_bot.application.assistant.reply_filter import normalize_ai_reply_text
from fogmoe_bot.application.assistant.inference.service import ASSISTANT_INFERENCE_SERVICE
from fogmoe_bot.application.assistant.agent_loop import DEFAULT_AGENT_LOOP
from fogmoe_bot.application.telegram.sticker_sender import normalize_sticker_directives, send_ai_reply_with_stickers
from fogmoe_bot.application.telegram.assistant_visible_sender import TelegramVisibleContentHandler
from fogmoe_bot.domain.agent_runtime.history import tool_logs_to_record_entries

logger = logging.getLogger(__name__)

SCHEDULE_POLL_INTERVAL = 60
SCHEDULE_BATCH_SIZE = 5

_schedule_lock = asyncio.Lock()


def _recurrence_delta(unit: str, interval: int) -> Optional[timedelta]:
    if unit == "minute":
        return timedelta(minutes=interval)
    if unit == "hour":
        return timedelta(hours=interval)
    if unit == "day":
        return timedelta(days=interval)
    return None


def _calculate_next_run_at(
    previous_run_at: datetime,
    recurrence_unit: str,
    recurrence_interval: int,
) -> Optional[datetime]:
    delta = _recurrence_delta(recurrence_unit, recurrence_interval)
    if delta is None:
        return None

    if previous_run_at.tzinfo is not None:
        previous_run_at = previous_run_at.astimezone(timezone.utc).replace(tzinfo=None)

    now = datetime.utcnow()
    next_run_at = previous_run_at + delta
    while next_run_at <= now:
        next_run_at += delta
    return next_run_at


def _format_scheduled_message(
    *,
    timestamp: datetime,
    scheduled_at: Optional[datetime],
    scheduled_for: Optional[datetime],
    trigger_reason: str,
    context_text: Optional[str],
    instruction: str,
) -> str:
    """@brief 兼容旧调用的定时任务渲染薄封装 / Thin compatibility wrapper for scheduled task rendering."""

    return DEFAULT_AGENT_LOOP.context_builder.render_scheduled_task(
        ScheduledTaskContext(
            timestamp=timestamp,
            scheduled_at=scheduled_at,
            scheduled_for=scheduled_for,
            trigger_reason=trigger_reason,
            context_text=context_text,
            instruction=instruction,
        )
    )


async def _handle_overflow_summary(conversation_id: int, level: Optional[str]) -> None:
    if level != "overflow":
        return
    summary_text = await summary.generate_summary_immediately(conversation_id)
    if summary_text:
        await db_connection.async_update_latest_history_state_summary(
            conversation_id,
            summary_text,
        )
    else:
        summary.schedule_summary_generation(conversation_id)


async def _mark_schedule_status(
    schedule_id: int,
    status: str,
    *,
    error: Optional[str] = None,
) -> None:
    await ai_schedule_repository.mark_status(schedule_id, status, error=error)


async def _reschedule_recurring_task(
    schedule_id: int,
    last_run_at: datetime,
    next_run_at: datetime,
) -> None:
    await ai_schedule_repository.reschedule_recurring(
        schedule_id,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
    )


async def _persist_tool_logs(
    conversation_id: int,
    tool_logs: list[dict],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    tool_record_entries = tool_logs_to_record_entries(tool_logs)

    if tool_record_entries:
        snapshot_created, warning_level, archived_records = await db_connection.async_insert_chat_records(
            conversation_id,
            tool_record_entries,
        )
        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        await _handle_overflow_summary(conversation_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(conversation_id)


async def _claim_due_schedules(limit: int = SCHEDULE_BATCH_SIZE) -> list[tuple]:
    return await ai_schedule_repository.claim_due(limit)


async def _process_schedule_task(
    task_row: tuple,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user_id = int(task_row[1])
    async with CONVERSATION_LOCK_MANAGER.hold(user_id):
        await _process_schedule_task_locked(task_row, context)


async def _process_schedule_task_locked(
    task_row: tuple,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    (
        schedule_id,
        user_id,
        run_at,
        created_at,
        trigger_reason,
        context_text,
        instruction,
        recurrence_unit,
        recurrence_interval,
    ) = task_row
    if isinstance(trigger_reason, bytes):
        trigger_reason = trigger_reason.decode("utf-8", errors="ignore")
    if isinstance(context_text, bytes):
        context_text = context_text.decode("utf-8", errors="ignore")
    if isinstance(instruction, bytes):
        instruction = instruction.decode("utf-8", errors="ignore")
    if isinstance(recurrence_unit, bytes):
        recurrence_unit = recurrence_unit.decode("utf-8", errors="ignore")
    recurrence_unit = (recurrence_unit or "none").strip().lower()
    try:
        recurrence_interval = int(recurrence_interval or 1)
    except (TypeError, ValueError):
        recurrence_interval = 1
    if recurrence_interval < 1:
        recurrence_interval = 1

    try:
        user_state = await load_user_state(user_id)
        if user_state is None:
            await _mark_schedule_status(
                schedule_id,
                "failed",
                error="user not found",
            )
            return
        user_state_prompt = DEFAULT_AGENT_LOOP.context_builder.render_user_state(user_state)

        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        scheduled_message = _format_scheduled_message(
            timestamp=now_utc,
            scheduled_at=created_at,
            scheduled_for=run_at,
            trigger_reason=trigger_reason or "",
            context_text=context_text or "",
            instruction=instruction or "",
        )

        snapshot_created, warning_level, archived_records = await db_connection.async_insert_chat_record(
            user_id,
            "user",
            scheduled_message,
            system_prompt_extra=user_state_prompt,
        )
        if archived_records:
            await send_permanent_records_archive(
                context.bot,
                user_id,
                archived_records,
                logger=logger,
            )
        await _handle_overflow_summary(user_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(user_id)

        chat_history = await db_connection.async_get_chat_history(user_id)
        model_query = DEFAULT_AGENT_LOOP.context_builder.build_model_query(
            history_messages=chat_history,
            scope=ConversationScope(user_id=user_id),
            user_state_prompt=user_state_prompt,
        )

        try:
            await context.bot.send_chat_action(chat_id=user_id, action="typing")
        except Exception:
            logger.debug("Failed to send typing action for scheduled task %s", schedule_id)

        sent_messages: list = []
        send_func = partial_send(context.bot.send_message, user_id)
        visible_content_handler = TelegramVisibleContentHandler(
            loop=asyncio.get_running_loop(),
            bot=context.bot,
            chat_id=user_id,
            first_text_send=send_func,
            fallback_send=send_func,
            logger=logger,
        )

        assistant_message, tool_logs = await ASSISTANT_INFERENCE_SERVICE.infer(
            model_query.messages,
            user_id=user_id,
            tool_context=model_query.tool_context,
            text_fallback_messages=model_query.text_fallback_messages,
            visible_content_sink=visible_content_handler,
        )
        sent_messages.extend(visible_content_handler.sent_messages)
        assistant_message = normalize_ai_reply_text(assistant_message)
        if assistant_message.strip():
            assistant_message = await normalize_sticker_directives(
                assistant_message,
                logger=logger,
            )

        if tool_logs:
            await _persist_tool_logs(user_id, tool_logs, context, user_id)

        if assistant_message.strip():
            snapshot_created, warning_level, archived_records = await db_connection.async_insert_chat_record(
                user_id,
                "assistant",
                assistant_message,
            )
            if archived_records:
                await send_permanent_records_archive(
                    context.bot,
                    user_id,
                    archived_records,
                    logger=logger,
                )
            await _handle_overflow_summary(user_id, warning_level)
            if snapshot_created and warning_level != "overflow":
                summary.schedule_summary_generation(user_id)

            try:
                await context.bot.send_chat_action(chat_id=user_id, action="typing")
            except Exception:
                logger.debug("Failed to send typing action before scheduled AI reply")
            sent_messages.extend(
                await send_ai_reply_with_stickers(
                    bot=context.bot,
                    chat_id=user_id,
                    text=str(assistant_message),
                    first_text_send=send_func,
                    fallback_send=send_func,
                    logger=logger,
                )
            )
        sent_messages.extend(
            await send_generated_audio_from_tool_logs(
                bot=context.bot,
                chat_id=user_id,
                tool_logs=tool_logs,
                logger=logger,
            )
        )
        sent_messages.extend(
            await send_generated_images_from_tool_logs(
                bot=context.bot,
                chat_id=user_id,
                tool_logs=tool_logs,
                logger=logger,
            )
        )
        if not sent_messages and not assistant_message.strip():
            tool_log_types = [
                str(tool_log.get("type", "tool_result"))
                for tool_log in tool_logs
                if isinstance(tool_log, dict)
            ]
            logger.info(
                "Scheduled AI produced empty response; no Telegram message sent: user_id=%s schedule_id=%s tool_log_types=%s",
                user_id,
                schedule_id,
                tool_log_types,
            )
        next_run_at = _calculate_next_run_at(
            run_at,
            recurrence_unit,
            recurrence_interval,
        )
        if next_run_at is None:
            await _mark_schedule_status(schedule_id, "executed")
        else:
            await _reschedule_recurring_task(schedule_id, run_at, next_run_at)
    except Exception as exc:
        logger.exception("Scheduled task %s failed: %s", schedule_id, exc)
        error_text = str(exc)
        if len(error_text) > 500:
            error_text = error_text[:500]
        await _mark_schedule_status(schedule_id, "failed", error=error_text)


async def run_ai_schedule_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _schedule_lock.locked():
        return

    async with _schedule_lock:
        tasks = await _claim_due_schedules()
        if not tasks:
            return
        for task in tasks:
            await _process_schedule_task(task, context)
