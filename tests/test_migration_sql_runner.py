from pathlib import Path

from fogmoe_bot.infrastructure.database.migrations import runner


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
