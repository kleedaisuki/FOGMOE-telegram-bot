"""@brief 后台调度守护注册与运行 / Background-scheduling daemon registration and runtime."""

import logging

from telegram.ext import ContextTypes

from fogmoe_bot.application.assistant.prompt_job_handler import PromptJobHandler
from fogmoe_bot.domain.scheduling import ScheduleDispatcher
from fogmoe_bot.infrastructure.database.repositories.schedule_repository import ScheduleRepository


logger = logging.getLogger(__name__)

SCHEDULING_POLL_INTERVAL_SECONDS = 60
"""@brief 后台调度轮询间隔 / Background scheduling polling interval."""

SCHEDULING_DISPATCHER_KEY = "scheduling_dispatcher"
"""@brief Application bot_data 中的分派器键 / Dispatcher key in Application bot_data."""


def register_scheduling_daemon(application) -> None:
    """@brief 注册进程内后台调度守护器 / Register the in-process scheduling daemon.

    @param application PTB Application / PTB Application.
    @return None / None.
    """

    dispatcher = ScheduleDispatcher(
        repository=ScheduleRepository(),
        handlers=(PromptJobHandler(application.bot),),
    )
    application.bot_data[SCHEDULING_DISPATCHER_KEY] = dispatcher
    application.job_queue.run_repeating(
        run_scheduling_daemon_tick,
        interval=SCHEDULING_POLL_INTERVAL_SECONDS,
        first=5,
        job_kwargs={
            "misfire_grace_time": SCHEDULING_POLL_INTERVAL_SECONDS,
            "coalesce": True,
            "max_instances": 1,
        },
    )


async def run_scheduling_daemon_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 将 PTB tick 转发给调度应用服务 / Forward a PTB tick to the scheduling service.

    @param context PTB 回调上下文 / PTB callback context.
    @return None / None.
    """

    dispatcher = context.application.bot_data.get(SCHEDULING_DISPATCHER_KEY)
    if not isinstance(dispatcher, ScheduleDispatcher):
        raise RuntimeError("Scheduling dispatcher is not registered")
    report = await dispatcher.tick()
    if report.claimed or report.skipped:
        logger.info(
            "Scheduling tick completed: claimed=%s succeeded=%s failed=%s skipped=%s",
            report.claimed,
            report.succeeded,
            report.failed,
            report.skipped,
        )
