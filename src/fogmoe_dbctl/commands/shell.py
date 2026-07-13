"""@brief FogMoe PostgreSQL 交互 shell / Interactive FogMoe PostgreSQL shell."""

from __future__ import annotations

import argparse
import subprocess
from typing import Any

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import direct_psql_environment


def configure_parser(subparsers: Any) -> None:
    """@brief 注册 psql shell 子命令 / Register the psql shell command.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "shell",
        help="Open psql through the configured maintenance connection.",
        description=(
            "Open an interactive psql session using the endpoint and maintenance "
            "credentials from config.json."
        ),
    )
    parser.add_argument(
        "--no-psqlrc",
        action="store_true",
        help="Ignore system and user psql startup files.",
    )
    parser.set_defaults(handler=execute)


def build_command(*, no_psqlrc: bool) -> list[str]:
    """@brief 构造交互 psql 命令 / Build the interactive psql command.

    @param no_psqlrc 是否忽略 psqlrc / Whether to ignore psqlrc.
    @return 不含连接或密码参数的 subprocess argv / Subprocess argv without connection or password arguments.
    """

    command = [
        "psql",
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


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 以前台 TTY 启动 psql / Start psql attached to the foreground TTY.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @note 密码仅存在于子进程环境，永不进入 argv。/
        The password exists only in the child environment and never enters argv.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    environment = direct_psql_environment(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )
    environment["PGAPPNAME"] = "fogmoe-dbctl-shell"
    result = subprocess.run(
        build_command(no_psqlrc=args.no_psqlrc),
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


__all__ = ["build_command", "configure_parser", "execute"]
