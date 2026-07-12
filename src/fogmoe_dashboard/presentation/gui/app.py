"""@brief FogMoe Dashboard GUI composition root / FogMoe Dashboard GUI composition root."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from fogmoe_dashboard.api import DashboardClient
from fogmoe_dashboard.config import DEFAULT_CONFIG_DIR
from fogmoe_dashboard.presentation.duration import parse_duration
from fogmoe_dashboard.presentation.gui.window import DashboardWindow
from fogmoe_dashboard.presentation.gui.worker import DashboardFactory


def build_parser() -> argparse.ArgumentParser:
    """@brief 构造 GUI 命令行参数 / Build GUI command-line arguments.

    @return GUI 参数解析器 / GUI argument parser.
    """

    parser = argparse.ArgumentParser(
        prog="fogmoe-dashboard-gui",
        description="Explore FogMoe observability data in a native Qt dashboard.",
    )
    parser.add_argument(
        "--database-url",
        help="Explicit PostgreSQL URL; overrides service files.",
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--service", default="fogmoe_automation")
    parser.add_argument(
        "--window", default="1h", help="Initial window such as 15m or 7d."
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--auto-refresh",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Enable auto-refresh at this interval; zero disables it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """@brief 运行原生 Qt Dashboard / Run the native Qt Dashboard.

    @param argv 可替换命令参数 / Replaceable command arguments.
    @return None / None.
    """

    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        initial_window = parse_duration(args.window)
        if args.auto_refresh != 0 and not 2 <= args.auto_refresh <= 300:
            raise ValueError("auto-refresh must be zero or between 2 and 300 seconds")
        factory = _client_factory(args)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"fogmoe-dashboard-gui: error: {error}\n")
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(["fogmoe-dashboard-gui"])
    application.setApplicationName("FogMoe Dashboard")
    application.setOrganizationName("FogMoe")
    window = DashboardWindow(
        factory,
        initial_window=initial_window,
        auto_refresh_seconds=args.auto_refresh,
    )
    window.show()
    if owns_application:
        raise SystemExit(application.exec())


def _client_factory(args: argparse.Namespace) -> DashboardFactory:
    """@brief 构造仅在 worker 调用的 client 工厂 / Build a client factory invoked only by the worker."""

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if database_url:
        return lambda: DashboardClient.from_database_url(
            database_url,
            command_timeout=args.timeout,
        )
    config_dir = args.config_dir.resolve()
    service = str(args.service)
    timeout = float(args.timeout)
    return lambda: DashboardClient.from_environment(
        config_dir=config_dir,
        service_name=service,
        command_timeout=timeout,
    )


__all__ = ["build_parser", "main"]
