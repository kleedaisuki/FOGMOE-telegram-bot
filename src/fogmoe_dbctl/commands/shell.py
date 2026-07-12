"""@brief FogMoe PostgreSQL 交互 shell / Interactive FogMoe PostgreSQL shell."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

from fogmoe_dbctl.config import DEFAULT_CONFIG_DIR
from fogmoe_dbctl.postgres import psql_environment


_DATABASE = "fogmoe"
"""@brief shell 唯一目标数据库 / Sole shell target database."""
_SERVICE = "fogmoe_automation"
"""@brief shell 唯一 automation service / Sole shell automation service."""


def configure_parser(subparsers: Any) -> None:
    """@brief 注册 psql shell 子命令 / Register the psql shell command.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "shell",
        aliases=["psql"],
        help="Open psql on the FogMoe database through fogmoe_automation.",
        description=(
            "Open an interactive psql session on database fogmoe. Connection host, "
            "port, role, and password come from the configured libpq service."
        ),
    )
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument(
        "--no-psqlrc",
        action="store_true",
        help="Ignore system and user psql startup files.",
    )
    parser.set_defaults(handler=execute)


def build_command(
    *,
    no_psqlrc: bool,
) -> list[str]:
    """@brief 构造交互 psql 命令 / Build the interactive psql command.

    @param no_psqlrc 是否忽略 psqlrc / Whether to ignore psqlrc.
    @return subprocess argv / Subprocess argv.
    """

    command = [
        "psql",
        "--dbname",
        _DATABASE,
        "--set",
        "ON_ERROR_STOP=1",
        "--set",
        "HISTCONTROL=ignoredups",
        "--set",
        "PROMPT1=fogmoe:%n@%/%R%x%# ",
    ]
    if no_psqlrc:
        command.append("--no-psqlrc")
    return command


def execute(args: argparse.Namespace) -> None:
    """@brief 以前台 TTY 启动 psql / Start psql attached to the foreground TTY.

    @param args CLI 参数 / CLI arguments.
    @return None / None.
    @note 密码仅由 PGPASSFILE 读取，不进入 argv 或 PGPASSWORD。/
        Passwords are read only through PGPASSFILE, never argv or PGPASSWORD.
    """

    config_dir = args.config_dir.resolve()
    environment = psql_environment(config_dir, service_name=_SERVICE)
    environment["PGAPPNAME"] = "fogmoe-dbctl-shell"
    command = build_command(
        no_psqlrc=args.no_psqlrc,
    )
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


__all__ = ["build_command", "configure_parser", "execute"]
