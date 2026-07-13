"""@brief FogMoe Bot 进程组合根 / FogMoe Bot process composition root."""

from __future__ import annotations

import argparse
from pathlib import Path

from fogmoe_bot.config import (
    BotSettings,
    default_config_path,
    read_bot_settings,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.network.proxy import configure_proxy_environment
from fogmoe_bot.infrastructure.observability.composition import (
    ObservabilityAssembly,
    build_observability,
)
from fogmoe_bot.infrastructure.observability.logging import (
    configure_litellm_logging,
    configure_logging,
    prepare_litellm_logging,
    shutdown_logging,
)
from fogmoe_bot.resources import BotResources, load_resources


def _parse_arguments() -> argparse.Namespace:
    """@brief 解析 Bot 进程命令行 / Parse Bot-process command-line arguments.

    @return 仅包含根 JSONC 配置路径的参数 / Arguments containing only the root JSONC path.
    """

    parser = argparse.ArgumentParser(description="Run the FogMoe Telegram bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="root JSONC configuration file (default: project-root/config.json)",
    )
    return parser.parse_args()


def _log_directory(config_path: Path, settings: BotSettings) -> Path:
    """@brief 解析相对于根配置的日志目录 / Resolve the log directory relative to root configuration.

    @param config_path 用户指定的根 JSONC 文件 / User-specified root JSONC file.
    @param settings 已验证的 Bot 设置 / Validated Bot settings.
    @return 绝对或配置文件相对的日志目录 / Absolute or configuration-file-relative log directory.
    """

    configured = Path(settings.logging.directory)
    return configured if configured.is_absolute() else config_path.parent / configured


def run(
    *,
    settings: BotSettings,
    resources: BotResources,
    observability: ObservabilityAssembly,
) -> None:
    """@brief 延迟导入并运行 Telegram 应用 / Lazily import and run the Telegram application.

    @param settings 已验证的 Bot 设置 / Validated Bot settings.
    @param resources 组合根加载的只读资源 / Read-only resources loaded by the composition root.
    @param observability 进程唯一可观测性装配 / Sole process observability assembly.
    @return None / None.
    """

    from fogmoe_bot.presentation.telegram.bot_app import run as run_application

    configure_litellm_logging(settings.logging)
    run_application(
        observability,
        settings=settings,
        resources=resources,
    )


def main() -> None:
    """@brief 装配 JSONC、日志、遥测与 Telegram 进程 / Compose JSONC, logging, telemetry, and the Telegram process.

    @return None / None.
    """

    arguments = _parse_arguments()
    config_path = arguments.config.resolve()
    settings = read_bot_settings(config_path)
    resources = load_resources(log_directory=_log_directory(config_path, settings))
    db.configure_database(settings.database)
    observability = build_observability(
        settings=settings.observability,
        database=settings.database,
    )
    configure_logging(
        settings.logging, resources.log_directory, observability.telemetry
    )
    try:
        prepare_litellm_logging()
        from fogmoe_bot.infrastructure.llm.litellm_client import (
            configure_litellm_transport,
        )

        configure_proxy_environment(settings.network)
        configure_litellm_transport(settings.network)
        run(settings=settings, resources=resources, observability=observability)
    finally:
        shutdown_logging()


if __name__ == "__main__":
    main()
