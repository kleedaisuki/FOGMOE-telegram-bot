import logging

from telegram.ext import ApplicationBuilder

from fogmoe_bot.infrastructure import config
from fogmoe_bot.application.moderation.member_verify import restore_verification_tasks
from fogmoe_bot.application.telegram.bot_conversation import post_init as conversation_post_init

from .handler_registry import register_handlers


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
    application = create_application()
    try:
        application.run_polling(timeout=config.TELEGRAM_GET_UPDATES_TIMEOUT)
    except KeyboardInterrupt:
        logging.info("Bot shutdown requested by keyboard interrupt.")
