"""@brief Memory、Context Window 与 Conversation 边界测试 / Memory, context-window, and conversation boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "fogmoe_bot"
"""@brief Bot 源码根目录 / Bot source root."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _imports(path: Path) -> tuple[str, ...]:
    """@brief 提取模块的绝对 import targets / Extract absolute import targets from a module.

    @param path Python 源文件 / Python source file.
    @return import target tuple / Import-target tuple.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            targets.append(node.module)
    return tuple(targets)


def _python_files(root: Path) -> tuple[Path, ...]:
    """@brief 返回稳定排序的 Python 文件 / Return deterministically ordered Python files.

    @param root 包根目录 / Package root.
    @return Python file tuple / Python-file tuple.
    """

    return tuple(sorted(root.rglob("*.py")))


def test_conversation_domain_does_not_depend_on_memory_management() -> None:
    """@brief Conversation 事实模型不能依赖 Memory 或 Context Window / Conversation facts cannot depend on memory or context-window management."""

    forbidden = (
        "fogmoe_bot.domain.memory",
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.application.memory",
        "fogmoe_bot.application.context_window",
    )
    violations = [
        (path, target)
        for path in _python_files(SRC_ROOT / "domain" / "conversation")
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []


def test_memory_and_context_window_aggregates_are_independent() -> None:
    """@brief Memory 与 Context Window 不互相导入 aggregate / Memory and context-window aggregates do not import each other."""

    rules = {
        SRC_ROOT / "domain" / "memory": "fogmoe_bot.domain.context_window",
        SRC_ROOT / "domain" / "context_window": "fogmoe_bot.domain.memory",
        SRC_ROOT / "application" / "memory": "fogmoe_bot.application.context_window",
        SRC_ROOT / "application" / "context_window": "fogmoe_bot.application.memory",
    }
    violations = [
        (path, target)
        for root, forbidden in rules.items()
        for path in _python_files(root)
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []


def test_removed_retention_paths_have_no_compatibility_facades() -> None:
    """@brief 已删除 retention 路径不保留兼容 facade / Removed retention paths retain no compatibility facades."""

    removed = (
        SRC_ROOT / "domain" / "conversation" / "retention.py",
        SRC_ROOT / "domain" / "context_window" / "retention.py",
        SRC_ROOT / "application" / "conversation" / "history_projection.py",
        SRC_ROOT / "application" / "conversation" / "history_cache.py",
        SRC_ROOT / "application" / "conversation" / "compaction_worker.py",
        SRC_ROOT / "infrastructure" / "database" / "conversation_retention.py",
        SRC_ROOT / "infrastructure" / "assistant" / "compaction_summary.py",
        SRC_ROOT / "infrastructure" / "llm" / "history_token_counter.py",
    )
    assert [path for path in removed if path.exists()] == []


def test_assistant_memory_operation_depends_only_on_memory_port_and_dto() -> None:
    """@brief Assistant memory operation 不依赖 compaction 或数据库 / Assistant memory operation does not depend on compaction or database models."""

    path = SRC_ROOT / "infrastructure" / "assistant" / "tool_operations" / "memory.py"
    forbidden = (
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.infrastructure.database",
    )
    assert [target for target in _imports(path) if target.startswith(forbidden)] == []


def test_database_snapshot_expresses_separate_memory_and_context_ownership() -> None:
    """@brief Schema snapshot 不再使用 nullable retention union / The schema snapshot no longer uses a nullable retention union."""

    snapshot = (PROJECT_ROOT / "src" / "fogmoe_dbctl" / "schema.sql").read_text(
        encoding="utf-8"
    )
    migration = (
        PROJECT_ROOT
        / "src"
        / "fogmoe_dbctl"
        / "migrations"
        / "sql"
        / "postgresql"
        / "0040_memory_context_boundaries.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE context_window.compactions" in snapshot
    assert "CREATE TABLE memory.records" in snapshot
    assert "conversation.retention_segments" not in snapshot
    assert "INSERT INTO memory.records" in migration
    assert "DELETE FROM conversation.retention_segments" in migration
