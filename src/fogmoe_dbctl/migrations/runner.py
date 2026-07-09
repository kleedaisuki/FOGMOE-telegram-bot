from __future__ import annotations

import re
from pathlib import Path

from alembic import op

SECTION_RE = re.compile(r"^\s*--\s*migrate:(up|down)\s*$", re.IGNORECASE)


class MigrationSqlError(RuntimeError):
    """@brief 迁移 SQL 错误 / Migration SQL error.

    @note 用于报告缺失文件、缺失段落或无法解析的 SQL / Reports missing files, missing sections, or unparsable SQL.
    """


def _migration_root() -> Path:
    """@brief 获取迁移根目录 / Get migration root directory.

    @return migrations 目录路径 / Path to the migrations directory.
    """

    return Path(__file__).resolve().parent


def _dialect_candidates(dialect_name: str) -> list[str]:
    """@brief 生成后端 SQL 目录候选 / Build backend SQL directory candidates.

    @param dialect_name SQLAlchemy dialect 名称 / SQLAlchemy dialect name.
    @return 候选目录名 / Candidate directory names.
    """

    normalized = (dialect_name or "").lower()
    candidates = ["postgresql" if normalized in {"postgres", "postgresql"} else normalized]
    candidates.append("generic")
    return [candidate for candidate in candidates if candidate]


def _current_dialect_name() -> str:
    """@brief 获取当前数据库后端名称 / Get current database backend name.

    @return SQLAlchemy dialect 名称 / SQLAlchemy dialect name.
    """

    try:
        return op.get_bind().dialect.name
    except Exception:
        return op.get_context().dialect.name


def _sql_file_for_revision(revision_file: str | Path) -> Path:
    """@brief 定位 revision 对应 SQL 文件 / Locate SQL file for a revision.

    @param revision_file Alembic revision 文件路径 / Alembic revision file path.
    @return SQL 文件路径 / SQL file path.
    """

    revision_name = Path(revision_file).stem
    sql_root = _migration_root() / "sql"
    dialect_name = _current_dialect_name()

    for dialect in _dialect_candidates(dialect_name):
        path = sql_root / dialect / f"{revision_name}.sql"
        if path.exists():
            return path

    searched = ", ".join(
        str(sql_root / dialect / f"{revision_name}.sql")
        for dialect in _dialect_candidates(dialect_name)
    )
    raise MigrationSqlError(f"missing SQL migration for {revision_name}; searched: {searched}")


def _render_template(sql: str) -> str:
    """@brief 渲染迁移 SQL 模板变量 / Render migration SQL template variables.

    @param sql 原始 SQL / Raw SQL.
    @return 渲染后的 SQL / Rendered SQL.
    @note 仅支持稳定、显式的小集合变量 / Only supports a small explicit set of stable variables.
    """

    if "{{" not in sql:
        return sql

    from fogmoe_dbctl import config

    replacements = {
        "{{ admin_user_id }}": str(config.admin_user_id()),
    }
    for token, value in replacements.items():
        sql = sql.replace(token, value)
    return sql


def _sections(sql_text: str, path: Path) -> dict[str, str]:
    """@brief 解析 up/down 段落 / Parse up/down sections.

    @param sql_text SQL 文件内容 / SQL file content.
    @param path SQL 文件路径 / SQL file path.
    @return 段落映射 / Section mapping.
    """

    sections: dict[str, list[str]] = {"up": [], "down": []}
    current: str | None = None

    for line in sql_text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1).lower()
            continue
        if current is not None:
            sections[current].append(line)

    parsed = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    if not parsed["up"] and not parsed["down"]:
        raise MigrationSqlError(f"{path} must contain -- migrate:up or -- migrate:down sections")
    return parsed


def _split_sql_statements(sql: str) -> list[str]:
    """@brief 拆分 SQL 语句 / Split SQL statements.

    @param sql SQL 段落 / SQL section.
    @return 单条 SQL 语句列表 / List of individual SQL statements.
    """

    statements: list[str] = []
    chars: list[str] = []
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    idx = 0

    while idx < len(sql):
        char = sql[idx]
        next_char = sql[idx + 1] if idx + 1 < len(sql) else ""

        if in_line_comment:
            chars.append(char)
            if char == "\n":
                in_line_comment = False
            idx += 1
            continue

        if in_block_comment:
            chars.append(char)
            if char == "*" and next_char == "/":
                chars.append(next_char)
                in_block_comment = False
                idx += 2
            else:
                idx += 1
            continue

        if quote:
            chars.append(char)
            if char == quote:
                if quote in {"'", '"'} and next_char == quote:
                    chars.append(next_char)
                    idx += 2
                    continue
                quote = None
            elif char == "\\" and quote in {"'", '"'} and next_char:
                chars.append(next_char)
                idx += 2
                continue
            idx += 1
            continue

        if char == "-" and next_char == "-":
            chars.append(char)
            chars.append(next_char)
            in_line_comment = True
            idx += 2
            continue
        if char == "/" and next_char == "*":
            chars.append(char)
            chars.append(next_char)
            in_block_comment = True
            idx += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            chars.append(char)
            idx += 1
            continue
        if char == ";":
            statement = "".join(chars).strip()
            if statement:
                statements.append(statement)
            chars = []
            idx += 1
            continue

        chars.append(char)
        idx += 1

    tail = "".join(chars).strip()
    if tail:
        statements.append(tail)
    return statements


def run_migration_sql(revision_file: str | Path, direction: str) -> None:
    """@brief 执行 revision 对应 SQL / Execute SQL for a revision.

    @param revision_file Alembic revision 文件路径 / Alembic revision file path.
    @param direction 迁移方向，up 或 down / Migration direction, up or down.
    @return None / None.
    """

    direction = direction.lower()
    if direction not in {"up", "down"}:
        raise ValueError(f"unsupported migration direction: {direction}")

    path = _sql_file_for_revision(revision_file)
    sql = _render_template(path.read_text(encoding="utf-8"))
    section = _sections(sql, path)[direction]
    if not section:
        return

    for statement in _split_sql_statements(section):
        op.execute(statement)
