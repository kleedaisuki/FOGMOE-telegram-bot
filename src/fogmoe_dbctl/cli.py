"""fogmoe-dbctl 组合根 / fogmoe-dbctl composition root."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fogmoe_dbctl.commands import bootstrap, export_csv, migrate, shell
from fogmoe_dbctl.config import default_config_path, read_dbctl_settings

COMMAND_MODULES = (bootstrap, migrate, export_csv, shell)


def build_parser() -> argparse.ArgumentParser:
    """@brief 构造完整 CLI 解析器 / Build the complete CLI parser.

    @return argparse 根解析器 / argparse root parser.
    """

    parser = argparse.ArgumentParser(
        prog="fogmoe-dbctl",
        description="Manage the external FogMoe PostgreSQL database.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to the root JSONC configuration file.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    for command_module in COMMAND_MODULES:
        command_module.configure_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """@brief 解析并执行 dbctl 子命令 / Parse and execute a dbctl subcommand.

    @param argv 命令行参数 / Command-line arguments.
    @return None / None.
    """

    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not args_list:
        parser.print_help()
        return

    args = parser.parse_args(args_list)
    handler: Any | None = getattr(args, "handler", None)
    if handler is None:
        parser.error("a command is required")
    settings = read_dbctl_settings(args.config)
    handler(args, settings=settings)
