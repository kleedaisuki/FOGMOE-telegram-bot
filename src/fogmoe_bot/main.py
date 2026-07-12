from fogmoe_bot.infrastructure.observability.logging import (
    configure_litellm_logging,
    configure_logging,
    prepare_litellm_logging,
    shutdown_logging,
)
from fogmoe_bot.infrastructure.observability.composition import (
    ObservabilityAssembly,
    build_observability,
)
from fogmoe_bot.infrastructure.network.proxy import configure_proxy_environment


def run(observability: ObservabilityAssembly) -> None:
    """@brief 延迟导入并运行 Telegram 应用 / Lazily import and run the Telegram application.

    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return None / None.
    """
    from fogmoe_bot.presentation.telegram.bot_app import run as run_application

    configure_litellm_logging()
    run_application(observability)


def main() -> None:
    """@brief 装配日志、遥测与 Telegram 进程 / Compose logging, telemetry, and the Telegram process.

    @return None / None.
    """

    observability = build_observability()
    configure_logging(observability.telemetry)
    try:
        prepare_litellm_logging()
        configure_proxy_environment()
        run(observability)
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
