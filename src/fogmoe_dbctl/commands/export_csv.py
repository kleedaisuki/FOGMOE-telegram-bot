"""@brief PostgreSQL 表 CSV 导出子命令 / PostgreSQL table CSV export subcommand."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import (
    direct_psql_environment,
    quote_identifier,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
"""@brief 支持的 PostgreSQL 标识符模式 / Supported PostgreSQL identifier pattern."""


def configure_parser(subparsers: Any) -> None:
    """@brief 注册 CSV 导出子命令 / Register the CSV export subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "export-csv",
        help="Export one PostgreSQL table to a UTF-8 CSV file.",
        description=(
            "Export one schema-qualified PostgreSQL table through the configured "
            "maintenance connection. Arbitrary SQL is intentionally not accepted."
        ),
    )
    parser.add_argument(
        "--table",
        required=True,
        help="Schema-qualified table, e.g. conversation.chat_records.",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Destination CSV path."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output file atomically.",
    )
    parser.set_defaults(handler=execute)


def parse_table_name(raw_table: str) -> tuple[str, str]:
    """@brief 解析并校验 schema.table / Parse and validate schema.table.

    @param raw_table 原始表名 / Raw table name.
    @return schema 与表名 / Schema and table name.
    @raise ValueError 表名不是受支持的 schema.table 标识符时抛出 /
        Raised when the name is not a supported schema.table identifier.
    """

    parts = raw_table.split(".")
    if len(parts) != 2 or not all(
        _IDENTIFIER_PATTERN.fullmatch(part) for part in parts
    ):
        raise ValueError(
            "table must be a schema-qualified PostgreSQL identifier, "
            "for example conversation.chat_records"
        )
    return parts[0], parts[1]


def build_copy_sql(schema: str, table: str) -> str:
    """@brief 构造安全的 CSV COPY 查询 / Build a safe CSV COPY query.

    @param schema PostgreSQL schema 名 / PostgreSQL schema name.
    @param table PostgreSQL table 名 / PostgreSQL table name.
    @return 输出至标准输出的 COPY SQL / COPY SQL writing to standard output.
    """

    return (
        f"COPY (SELECT * FROM {quote_identifier(schema)}.{quote_identifier(table)}) "
        "TO STDOUT WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8');"
    )


def export_table(
    *,
    settings: DbctlSettings,
    schema: str,
    table: str,
    output_path: Path,
    force: bool,
) -> None:
    """@brief 原子导出一张表 / Export one table atomically.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param schema PostgreSQL schema 名 / PostgreSQL schema name.
    @param table PostgreSQL 表名 / PostgreSQL table name.
    @param output_path 目标 CSV 路径 / Destination CSV path.
    @param force 是否替换已有文件 / Whether to replace an existing file.
    @return None / None.
    @note 失败时保留原有目标文件，临时文件会被删除。/
        On failure, an existing destination is preserved and the temporary file is removed.
    """

    if output_path.exists() and not force:
        raise FileExistsError(
            f"output already exists: {output_path}; use --force to replace it"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sql = build_copy_sql(schema, table)
    command = [
        "psql",
        "--no-psqlrc",
        "--quiet",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
    ]
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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            subprocess.run(
                command,
                stdout=output_file,
                env=environment,
                check=True,
            )
        temporary_path.replace(output_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 执行 CSV 导出用例 / Execute the CSV export use case.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    schema, table = parse_table_name(args.table)
    output_path = args.output.resolve()
    export_table(
        settings=settings,
        schema=schema,
        table=table,
        output_path=output_path,
        force=args.force,
    )
    print(f"Exported {schema}.{table} to {output_path}")


__all__ = [
    "build_copy_sql",
    "configure_parser",
    "execute",
    "export_table",
    "parse_table_name",
]
