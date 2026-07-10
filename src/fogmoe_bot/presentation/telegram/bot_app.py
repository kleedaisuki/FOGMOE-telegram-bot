import logging
import time

import telegram.error
from telegram.ext import ApplicationBuilder

from fogmoe_bot.infrastructure import config
from fogmoe_bot.application.moderation.member_verify import restore_verification_tasks
from fogmoe_bot.application.telegram.bot_conversation import post_init as conversation_post_init

from .handler_registry import register_handlers


logger = logging.getLogger(__name__)


def _is_recoverable_polling_error(exc: BaseException) -> bool:
    """@brief 判断轮询启动失败能否恢复 / Decide whether a polling startup failure is recoverable.

    @param exc 轮询启动期间捕获的异常 / Exception raised while starting polling.
    @return 可通过重新创建 Application 恢复时返回 True / True if recreating the Application can recover.
    @note 配置、鉴权等永久错误必须立即暴露，不能被无限重试掩盖 /
    Configuration and authentication errors must remain visible rather than being retried forever.
    """

    return isinstance(
        exc,
        (
            telegram.error.NetworkError,
            telegram.error.TimedOut,
            telegram.error.RetryAfter,
        ),
    )


def _polling_retry_delay(attempt: int) -> float:
    """@brief 计算轮询恢复退避时间 / Calculate polling recovery backoff.

    @param attempt 从 1 开始的连续失败次数 / Consecutive failure count starting at one.
    @return 受上限约束的指数退避秒数 / Capped exponential-backoff delay in seconds.
    """

    initial_delay = max(0.0, config.TELEGRAM_POLLING_RETRY_INITIAL_DELAY)
    max_delay = max(initial_delay, config.TELEGRAM_POLLING_RETRY_MAX_DELAY)
    return min(max_delay, initial_delay * (2 ** max(0, attempt - 1)))


async def post_init(application) -> None:
    """@brief 初始化对话基础设施并恢复验证任务 / Initialize conversation state and restore verification tasks.

    @param application PTB Application / PTB Application.
    @return None / None.
    """

    await conversation_post_init(application)
    await restore_verification_tasks(application)


def create_application():
    builder = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(config.TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(config.TELEGRAM_READ_TIMEOUT)
        .write_timeout(config.TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(config.TELEGRAM_POOL_TIMEOUT)
        .get_updates_connect_timeout(config.TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT)
        .get_updates_read_timeout(config.TELEGRAM_GET_UPDATES_READ_TIMEOUT)
        .get_updates_write_timeout(config.TELEGRAM_GET_UPDATES_WRITE_TIMEOUT)
        .get_updates_pool_timeout(config.TELEGRAM_GET_UPDATES_POOL_TIMEOUT)
        .get_updates_connection_pool_size(config.TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE)
        .concurrent_updates(True)
        .post_init(post_init)
    )
    proxy_url = config.NETWORK_PROXY_URL
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    application = builder.build()

    register_handlers(application)
    return application


def run() -> None:
    """@brief 运行具备连接恢复的 Telegram 轮询 / Run Telegram polling with connection recovery.

    @return None / None.
    @note PTB 负责已运行轮询的网络重试；本循环额外恢复启动阶段的瞬态失败 /
    PTB retries transient failures of an active polling loop; this loop additionally
    recovers transient bootstrap failures by rebuilding the Application.
    """

    attempt = 0
    while True:
        application = create_application()
        try:
            application.run_polling(
                timeout=config.TELEGRAM_GET_UPDATES_TIMEOUT,
                bootstrap_retries=0,
            )
            return
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested by keyboard interrupt.")
            return
        except Exception as exc:
            if not _is_recoverable_polling_error(exc):
                raise

            attempt += 1
            delay = _polling_retry_delay(attempt)
            logger.warning(
                "Telegram polling bootstrap failed with a transient network error; "
                "rebuilding the application in %.1f seconds (attempt %s): %s",
                delay,
                attempt,
                exc,
                exc_info=True,
            )
            time.sleep(delay)
