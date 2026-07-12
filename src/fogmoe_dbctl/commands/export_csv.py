"""PostgreSQL 表 CSV 导出子命令 / PostgreSQL table CSV export subcommand."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fogmoe_dbctl.config import DEFAULT_CONFIG_DIR
from fogmoe_dbctl.postgres import quote_identifier


_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def configure_parser(subparsers: Any) -> None:
    """@brief 注册 CSV 导出子命令 / Register the CSV export subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "export-csv",
        aliases=["export"],
        help="Export one PostgreSQL table to a UTF-8 CSV file.",
        description=(
            "Export one schema-qualified PostgreSQL table through a configured "
            "psql service. Arbitrary SQL is intentionally not accepted."
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
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--service", default="fogmoe_automation")
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


def psql_environment(config_dir: Path) -> dict[str, str]:
    """@brief 构造 psql service 环境 / Build the psql service environment.

    @param config_dir 项目 psql 配置目录 / Project psql configuration directory.
    @return 供 psql 子进程使用的环境 / Environment for the psql subprocess.
    """

    environment = os.environ.copy()
    environment["PGSERVICEFILE"] = str(config_dir / "pg_service.conf")
    environment["PGPASSFILE"] = str(config_dir / "pgpass")
    return environment


def export_table(
    *,
    config_dir: Path,
    service_name: str,
    schema: str,
    table: str,
    output_path: Path,
    force: bool,
) -> None:
    """@brief 原子导出一张表 / Export one table atomically.

    @param config_dir 项目 psql 配置目录 / Project psql configuration directory.
    @param service_name psql service 名 / psql service name.
    @param schema PostgreSQL schema 名 / PostgreSQL schema name.
    @param table PostgreSQL table 名 / PostgreSQL table name.
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
        "--dbname",
        f"service={service_name}",
        "--command",
        sql,
    ]
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
                env=psql_environment(config_dir),
                check=True,
            )
        temporary_path.replace(output_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def execute(args: argparse.Namespace) -> None:
    """@brief 执行 CSV 导出用例 / Execute the CSV export use case.

    @param args CLI 参数 / CLI arguments.
    @return None / None.
    """

    schema, table = parse_table_name(args.table)
    output_path = args.output.resolve()
    export_table(
        config_dir=args.config_dir.resolve(),
        service_name=args.service,
        schema=schema,
        table=table,
        output_path=output_path,
        force=args.force,
    )
    print(f"Exported {schema}.{table} to {output_path}")
