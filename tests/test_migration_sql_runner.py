import ast
import re
from pathlib import Path

import pytest

from fogmoe_dbctl.migrations import runner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def test_migration_sql_sections_are_parsed():
    sql = """
-- migrate:up
CREATE TABLE demo (id INT);

-- migrate:down
DROP TABLE demo;
"""

    sections = runner._sections(sql, Path("demo.sql"))

    assert sections["up"] == "CREATE TABLE demo (id INT);"
    assert sections["down"] == "DROP TABLE demo;"


def test_sql_splitter_keeps_semicolon_inside_string_literal():
    statements = runner._split_sql_statements(
        "INSERT INTO demo (text) VALUES ('a;b'); ALTER TABLE demo ADD COLUMN name TEXT;"
    )

    assert statements == [
        "INSERT INTO demo (text) VALUES ('a;b')",
        "ALTER TABLE demo ADD COLUMN name TEXT",
    ]


def test_sql_splitter_keeps_function_body_as_one_statement() -> None:
    """@brief PostgreSQL function body 内分号不得拆迁移 / Semicolons in a PostgreSQL function body do not split a migration."""

    statements = runner._split_sql_statements(
        "CREATE FUNCTION demo() RETURNS void LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM 1; PERFORM 2; END; $$; SELECT 3;"
    )

    assert statements == [
        "CREATE FUNCTION demo() RETURNS void LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM 1; PERFORM 2; END; $$",
        "SELECT 3",
    ]


def test_migration_runner_preserves_colons_inside_quoted_account_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Driver SQL 保留账户键中的冒号 / Driver SQL preserves colons in quoted account keys.

    @param tmp_path 临时迁移目录 / Temporary migration directory.
    @param monkeypatch pytest 依赖替换器 / pytest dependency patcher.
    @return None / None.
    @note SQLAlchemy TextClause 会把 :free 识别为绑定参数；迁移不得经过该解析器。/
        SQLAlchemy TextClause recognizes :free as a bind parameter, so migrations must bypass that parser.
    """

    migration = tmp_path / "demo.py"
    sql_path = tmp_path / "demo.sql"
    sql_path.write_text(
        """-- migrate:up
SELECT 'user:42:free', 'system:staking_pool';

-- migrate:down
""",
        encoding="utf-8",
    )
    executed: list[str] = []

    class _Connection:
        """@brief 记录 driver SQL 的连接替身 / Connection double recording driver SQL."""

        def exec_driver_sql(self, statement: str) -> None:
            """@brief 记录一条原始 SQL / Record one raw SQL statement.

            @param statement 待执行 SQL / SQL to execute.
            @return None / None.
            """

            executed.append(statement)

    connection = _Connection()
    monkeypatch.setattr(runner, "_sql_file_for_revision", lambda _path: sql_path)
    monkeypatch.setattr(runner.op, "get_bind", lambda: connection)

    runner.run_migration_sql(migration, "up")

    assert executed == ["SELECT 'user:42:free', 'system:staking_pool'"]


def test_migration_template_quotes_the_injected_application_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 应用角色模板只能渲染为 PostgreSQL quoted identifier / The application-role template renders only as a PostgreSQL quoted identifier.

    @param monkeypatch pytest 依赖替换器 / Pytest dependency patcher.
    @return None / None.
    """

    monkeypatch.setattr(
        runner,
        "_injected_application_role",
        lambda: 'runtime"role',
    )

    rendered = runner._render_template(
        "GRANT EXECUTE ON FUNCTION observability.demo() TO {{ application_role }};"
    )

    assert rendered.endswith('TO "runtime""role";')
    assert "{{" not in rendered


def test_migration_template_quotes_application_role_as_a_literal_in_dynamic_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 动态 SQL 的角色值使用 literal 而非 identifier 表达式 / Dynamic SQL receives a role literal rather than an identifier expression.

    @param monkeypatch pytest 依赖替换器 / Pytest dependency patcher.
    @return None / None.
    """

    monkeypatch.setattr(
        runner,
        "_injected_application_role",
        lambda: "runtime'role",
    )

    rendered = runner._render_template(
        "SELECT format('GRANT TO %I', {{ application_role_literal }});"
    )

    assert rendered.endswith("'runtime''role');")
    assert "{{" not in rendered


def test_migration_template_rejects_unknown_tokens() -> None:
    """@brief 未知模板 token 必须失败关闭 / Unknown template tokens fail closed."""

    with pytest.raises(runner.MigrationSqlError, match="unsupported.*unknown"):
        runner._render_template("SELECT {{ unknown }};")


def test_schema_snapshot_head_matches_the_single_migration_graph_head() -> None:
    """@brief schema header 自动匹配 migration DAG 唯一 head / The schema header automatically matches the migration DAG's sole head."""

    versions = PROJECT_ROOT / "src/fogmoe_dbctl/migrations/versions"
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions.glob("*.py"):
        if path.name == "__init__.py":
            continue
        assignments: dict[str, object] = {}
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in {
                "revision",
                "down_revision",
            }:
                continue
            assignments[target.id] = ast.literal_eval(node.value)
        revision = assignments.get("revision")
        assert isinstance(revision, str), f"missing revision in {path.name}"
        revisions.add(revision)
        down_revision = assignments.get("down_revision")
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, tuple):
            assert all(isinstance(item, str) for item in down_revision)
            parents.update(down_revision)
        else:
            assert down_revision is None

    assert parents <= revisions
    heads = revisions - parents
    assert len(heads) == 1
    snapshot = (PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^-- Alembic head: (\S+)$", snapshot, re.MULTILINE)
    assert match is not None
    assert match.group(1) == next(iter(heads))


def test_observability_migration_and_snapshot_share_storage_contract() -> None:
    """@brief migration 与快照共享 observability 契约 / Migration and snapshot share the observability contract."""

    migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0039_observability.sql"
    ).read_text(encoding="utf-8")
    snapshot = (PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    for statement in (
        "CREATE SCHEMA IF NOT EXISTS observability",
        "CREATE TABLE observability.resources",
        "CREATE TABLE observability.log_records",
        "CREATE TABLE observability.spans",
        "CREATE TABLE observability.metric_points",
        "CREATE FUNCTION observability.ensure_daily_partitions",
        "CREATE FUNCTION observability.drop_partitions_before",
        "CREATE VIEW observability.pipeline_health",
        "CREATE VIEW observability.turn_latency",
    ):
        assert statement in migration
        assert statement in snapshot
    assert migration.count("ADD COLUMN traceparent VARCHAR(55)") == 3
    assert snapshot.count("traceparent VARCHAR(55) NOT NULL") == 3
