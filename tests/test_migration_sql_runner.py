import ast
from pathlib import Path
import re

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
