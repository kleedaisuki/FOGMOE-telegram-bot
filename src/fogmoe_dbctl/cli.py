"""fogmoe-dbctl 命令行入口 / fogmoe-dbctl command-line entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from fogmoe_dbctl import bootstrap_postgres, migrate_as_role

COMMANDS = {
    "bootstrap-postgres": bootstrap_postgres.main,
    "bootstrap": bootstrap_postgres.main,
    "migrate": migrate_as_role.main,
    "upgrade": migrate_as_role.main,
    "run-migrations-as-role": migrate_as_role.main,
}


def print_help() -> None:
    """@brief 打印 dbctl 帮助 / Print dbctl help.

    @return None / None.
    """

    print(
        "usage: fogmoe-dbctl <command> [args]\n\n"
        "Manage the external FogMoe PostgreSQL database.\n\n"
        "commands:\n"
        "  bootstrap-postgres      Create PostgreSQL roles, database, and psql service files.\n"
        "  migrate                 Run Alembic migrations through the automation role and grants.\n\n"
        "aliases:\n"
        "  bootstrap, upgrade, run-migrations-as-role"
    )


def main(argv: Sequence[str] | None = None) -> None:
    """@brief 执行 dbctl CLI / Execute dbctl CLI.

    @param argv 命令行参数 / Command-line arguments.
    @return None / None.
    """

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print_help()
        return

    command = args[0]
    handler = COMMANDS.get(command)
    if handler is None:
        print_help()
        raise SystemExit(f"unknown fogmoe-dbctl command: {command}")
    handler(args[1:])
