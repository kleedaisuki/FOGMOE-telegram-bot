from fogmoe_bot.infrastructure.logging.bot_logging import (
    configure_litellm_logging,
    configure_logging,
    prepare_litellm_logging,
    shutdown_logging,
)
from fogmoe_bot.infrastructure.network.proxy import configure_proxy_environment


def run() -> None:
    """@brief 延迟导入并运行 Telegram 应用 / Lazily import and run the Telegram application.

    @return None / None.
    """
    from fogmoe_bot.presentation.telegram.bot_app import run as run_application

    configure_litellm_logging()
    run_application()


def main() -> None:
    configure_logging()
    try:
        prepare_litellm_logging()
        configure_proxy_environment()
        run()
    finally:
        shutdown_logging()


if __name__ == '__main__':
    main()
