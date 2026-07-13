"""@brief Retrieval、Context Window 与未来 Memory 边界测试 / Retrieval, context-window, and future-memory boundary tests."""

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


def test_conversation_domain_does_not_depend_on_derived_state() -> None:
    """@brief Conversation 事实模型不能依赖派生状态 / Conversation facts cannot depend on derived state."""

    forbidden = (
        "fogmoe_bot.domain.memory",
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.application.memory",
        "fogmoe_bot.application.context_window",
        "fogmoe_bot.domain.retrieval",
        "fogmoe_bot.application.retrieval",
    )
    violations = [
        (path, target)
        for path in _python_files(SRC_ROOT / "domain" / "conversation")
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []


def test_retrieval_domain_is_provider_and_product_independent() -> None:
    """@brief Retrieval domain 不依赖 Conversation、Context Window 或基础设施 / Retrieval domain is independent of conversation, context window, and infrastructure."""

    rules = {
        SRC_ROOT / "domain" / "retrieval": (
            "fogmoe_bot.domain.conversation",
            "fogmoe_bot.domain.context_window",
            "fogmoe_bot.application",
            "fogmoe_bot.infrastructure",
        ),
        SRC_ROOT / "domain" / "context_window": (
            "fogmoe_bot.domain.retrieval",
            "fogmoe_bot.application.retrieval",
        ),
        SRC_ROOT / "application" / "context_window": (
            "fogmoe_bot.domain.retrieval",
            "fogmoe_bot.application.retrieval",
        ),
    }
    violations = [
        (path, target)
        for root, forbidden in rules.items()
        for path in _python_files(root)
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []


def test_context_window_persistence_does_not_write_memory_records() -> None:
    """@brief Context Window checkpoint 不能形成 Memory record / Context-window checkpoints cannot form memory records.

    @return None / None.
    @note Compaction summary 只替换过长 Context State；User Profile 使用独立的未来机制。/
        A compaction summary only replaces oversized context state; a user profile uses an
        independent future mechanism.
    """

    context_window_store = (
        SRC_ROOT / "infrastructure" / "database" / "context_window.py"
    ).read_text(encoding="utf-8")
    assert "memory.records" not in context_window_store
    assert "_project_completed_compaction" not in context_window_store


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


def test_assistant_retrieval_operation_depends_only_on_port_and_dto() -> None:
    """@brief Assistant retrieval operation 不依赖 provider 或数据库 / Assistant retrieval operation does not depend on provider or database."""

    path = (
        SRC_ROOT / "infrastructure" / "assistant" / "tool_operations" / "retrieval.py"
    )
    forbidden = (
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.infrastructure.database",
    )
    assert [target for target in _imports(path) if target.startswith(forbidden)] == []


def test_database_snapshot_expresses_retrieval_and_context_ownership() -> None:
    """@brief Schema snapshot 将检索与 Context checkpoint 分离 / The schema separates retrieval from context checkpoints."""

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
        / "0041_episodic_retrieval.sql"
    ).read_text(encoding="utf-8")
    cleanup_migration = (
        PROJECT_ROOT
        / "src"
        / "fogmoe_dbctl"
        / "migrations"
        / "sql"
        / "postgresql"
        / "0042_remove_legacy_memory.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE context_window.compactions" in snapshot
    assert "CREATE TABLE retrieval.embedding_spaces" in snapshot
    assert "CREATE TABLE retrieval.passages" in snapshot
    assert "CREATE TABLE retrieval.passage_vectors" in snapshot
    assert "CREATE TABLE memory.records" not in snapshot
    assert "CREATE SCHEMA IF NOT EXISTS memory" not in snapshot
    assert "permanent_records_limit" not in snapshot
    assert "conversation.retention_segments" not in snapshot
    assert "DROP TABLE memory.records" in migration
    assert "DROP SCHEMA memory" in cleanup_migration
    assert "DROP COLUMN permanent_records_limit" in cleanup_migration
    assert "embedding vector(1024)" in snapshot
