"""@brief Assistant 定时回合处理器 / Scheduled Assistant-turn handler."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fogmoe_bot.application.accounts.context import load_user_state
from fogmoe_bot.application.assistant.inference.service import ASSISTANT_INFERENCE_SERVICE
from fogmoe_bot.application.assistant.reply_filter import normalize_ai_reply_text
from fogmoe_bot.application.assistant.tasks import summary
from fogmoe_bot.application.conversation_lock_manager import CONVERSATION_LOCK_MANAGER
from fogmoe_bot.application.telegram.archive_utils import send_permanent_records_archive
from fogmoe_bot.application.telegram.assistant_visible_sender import TelegramVisibleContentHandler
from fogmoe_bot.application.telegram.generated_audio_sender import send_generated_audio_from_tool_logs
from fogmoe_bot.application.telegram.generated_image_sender import send_generated_images_from_tool_logs
from fogmoe_bot.application.telegram.sticker_sender import normalize_sticker_directives, send_ai_reply_with_stickers
from fogmoe_bot.domain.agent_runtime.history import tool_logs_to_record_entries
from fogmoe_bot.domain.context import (
    ConversationScope,
    ScheduledTaskContext,
    build_context_state,
    render_scheduled_task,
    render_user_state,
)
from fogmoe_bot.domain.scheduling import JobKind, PROMPT_JOB_KIND, PromptJobPayload, ScheduledJob
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.telegram.telegram_utils import partial_send


logger = logging.getLogger(__name__)


class PromptJobHandler:
    """@brief 将 Assistant 定时载荷执行为一次完整回合 / Execute an Assistant payload as a complete turn."""

    def __init__(self, bot: Any) -> None:
        """@brief 创建处理器 / Create the handler.

        @param bot Telegram Bot 投递端口实现 / Telegram Bot delivery implementation.
        """

        self._bot = bot

    @property
    def kind(self) -> JobKind:
        """@brief 返回处理器支持的任务类型 / Return the supported job kind.

        @return Assistant 定时回合类型 / Scheduled Assistant-turn kind.
        """

        return PROMPT_JOB_KIND

    async def handle(self, job: ScheduledJob[Any]) -> None:
        """@brief 执行已领取的 Assistant 定时回合 / Execute a claimed scheduled Assistant turn.

        @param job 已领取任务 / Claimed scheduled job.
        @return None / None.
        @raise TypeError 载荷类型与处理器不匹配时抛出 / Raised for an incompatible payload.
        """

        if not isinstance(job.payload, PromptJobPayload):
            raise TypeError(
                f"Expected PromptJobPayload, got {type(job.payload).__name__}"
            )
        async with CONVERSATION_LOCK_MANAGER.hold(job.owner_id):
            await self._execute_locked(job, job.payload)

    async def _execute_locked(
        self,
        job: ScheduledJob[PromptJobPayload],
        payload: PromptJobPayload,
    ) -> None:
        """@brief 在用户会话锁内执行回合 / Execute the turn under the user conversation lock.

        @param job 类型化任务 / Typed scheduled job.
        @param payload Assistant 回合载荷 / Assistant-turn payload.
        @return None / None.
        """

        user_state = await load_user_state(job.owner_id)
        if user_state is None:
            raise LookupError(f"Scheduled job owner not found: {job.owner_id}")
        user_state_prompt = render_user_state(user_state)
        scheduled_message = render_scheduled_task(
            ScheduledTaskContext(
                timestamp=datetime.now(timezone.utc),
                scheduled_at=job.created_at,
                scheduled_for=job.run_at,
                trigger_reason=payload.trigger_reason,
                context_text=payload.context_text,
                instruction=payload.instruction,
            )
        )

        snapshot_created, warning_level, archived_records = (
            await db_connection.async_insert_chat_record(
                job.owner_id,
                "user",
                scheduled_message,
                system_prompt_extra=user_state_prompt,
            )
        )
        if archived_records:
            await send_permanent_records_archive(
                self._bot,
                job.owner_id,
                archived_records,
                logger=logger,
            )
        await self._handle_overflow_summary(job.owner_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(job.owner_id)

        chat_history = await db_connection.async_get_chat_history(job.owner_id)
        context_state = build_context_state(
            system_prompt=config.SYSTEM_PROMPT,
            history_messages=chat_history,
            scope=ConversationScope(user_id=job.owner_id),
            user_state=user_state,
        )
        await self._send_chat_action(job.owner_id, "typing")

        send_func = partial_send(self._bot.send_message, job.owner_id)
        visible_content_handler = TelegramVisibleContentHandler(
            loop=asyncio.get_running_loop(),
            bot=self._bot,
            chat_id=job.owner_id,
            first_text_send=send_func,
            fallback_send=send_func,
            logger=logger,
        )
        assistant_message, tool_logs = await ASSISTANT_INFERENCE_SERVICE.infer(
            context_state,
            visible_content_sink=visible_content_handler,
        )
        sent_messages = list(visible_content_handler.sent_messages)
        assistant_message = normalize_ai_reply_text(assistant_message)
        if assistant_message.strip():
            assistant_message = await normalize_sticker_directives(
                assistant_message,
                logger=logger,
            )

        if tool_logs:
            await self._persist_tool_logs(job.owner_id, tool_logs)
        if assistant_message.strip():
            await self._persist_assistant_reply(job.owner_id, assistant_message)
            await self._send_chat_action(job.owner_id, "typing")
            sent_messages.extend(
                await send_ai_reply_with_stickers(
                    bot=self._bot,
                    chat_id=job.owner_id,
                    text=assistant_message,
                    first_text_send=send_func,
                    fallback_send=send_func,
                    logger=logger,
                )
            )

        sent_messages.extend(
            await send_generated_audio_from_tool_logs(
                bot=self._bot,
                chat_id=job.owner_id,
                tool_logs=tool_logs,
                logger=logger,
            )
        )
        sent_messages.extend(
            await send_generated_images_from_tool_logs(
                bot=self._bot,
                chat_id=job.owner_id,
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
                "Scheduled Assistant turn produced no Telegram output: owner_id=%s "
                "schedule_id=%s tool_log_types=%s",
                job.owner_id,
                job.schedule_id,
                tool_log_types,
            )

    async def _persist_tool_logs(self, owner_id: int, tool_logs: list[dict]) -> None:
        """@brief 持久化工具事件 / Persist tool events."""

        entries = tool_logs_to_record_entries(tool_logs)
        if not entries:
            return
        snapshot_created, warning_level, archived_records = (
            await db_connection.async_insert_chat_records(owner_id, entries)
        )
        if archived_records:
            await send_permanent_records_archive(
                self._bot,
                owner_id,
                archived_records,
                logger=logger,
            )
        await self._handle_overflow_summary(owner_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(owner_id)

    async def _persist_assistant_reply(self, owner_id: int, message: str) -> None:
        """@brief 持久化 Assistant 最终文本 / Persist final Assistant text."""

        snapshot_created, warning_level, archived_records = (
            await db_connection.async_insert_chat_record(owner_id, "assistant", message)
        )
        if archived_records:
            await send_permanent_records_archive(
                self._bot,
                owner_id,
                archived_records,
                logger=logger,
            )
        await self._handle_overflow_summary(owner_id, warning_level)
        if snapshot_created and warning_level != "overflow":
            summary.schedule_summary_generation(owner_id)

    async def _handle_overflow_summary(self, owner_id: int, level: str | None) -> None:
        """@brief 在历史溢出时同步生成摘要 / Generate a summary synchronously on history overflow."""

        if level != "overflow":
            return
        summary_text = await summary.generate_summary_immediately(owner_id)
        if summary_text:
            await db_connection.async_update_latest_history_state_summary(
                owner_id,
                summary_text,
            )
        else:
            summary.schedule_summary_generation(owner_id)

    async def _send_chat_action(self, owner_id: int, action: str) -> None:
        """@brief 尽力发送 Telegram 状态 / Best-effort Telegram chat action."""

        try:
            await self._bot.send_chat_action(chat_id=owner_id, action=action)
        except Exception:
            logger.debug(
                "Failed to send chat action for scheduled Assistant turn: owner_id=%s action=%s",
                owner_id,
                action,
            )
