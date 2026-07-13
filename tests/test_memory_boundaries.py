"""@brief Retrieval、Context Window 与 User Profile 边界测试 / Retrieval, context-window, and User Profile boundary tests."""

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
        "fogmoe_bot.domain.user_profile",
        "fogmoe_bot.application.user_profile",
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


def test_memory_domain_is_context_retrieval_and_storage_independent() -> None:
    """@brief Memory 产品领域不耦合 Context、Retrieval 或存储 / The Memory product domain is independent of Context, Retrieval, and storage."""

    forbidden = (
        "fogmoe_bot.domain.context",
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.domain.conversation",
        "fogmoe_bot.domain.retrieval",
        "fogmoe_bot.application",
        "fogmoe_bot.infrastructure",
    )
    violations = [
        (path, target)
        for path in _python_files(SRC_ROOT / "domain" / "memory")
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []


def test_user_profile_domain_is_conversation_provider_and_storage_independent() -> None:
    """@brief User Profile 领域不依赖来源、模型或存储 / User Profile domain is independent of source, model, and storage."""

    forbidden = (
        "fogmoe_bot.domain.conversation",
        "fogmoe_bot.domain.context_window",
        "fogmoe_bot.domain.retrieval",
        "fogmoe_bot.application",
        "fogmoe_bot.infrastructure",
    )
    violations = [
        (path, target)
        for path in _python_files(SRC_ROOT / "domain" / "user_profile")
        for target in _imports(path)
        if target.startswith(forbidden)
    ]
    assert violations == []
    for layer in ("domain", "application", "infrastructure"):
        package = SRC_ROOT / layer / "user_profile" / "__init__.py"
        assert "from ." not in package.read_text(encoding="utf-8")


def test_context_window_persistence_does_not_write_memory_records() -> None:
    """@brief Context Window checkpoint 不能形成 Memory record / Context-window checkpoints cannot form memory records.

    @return None / None.
    @note Compaction summary 只替换过长 Context State；User Profile 使用独立 Dreaming。/
        A compaction summary only replaces oversized context state; a user profile uses an
        independent Dreaming mechanism.
    """

    context_window_store = (
        SRC_ROOT / "infrastructure" / "database" / "context_window.py"
    ).read_text(encoding="utf-8")
    assert "memory.records" not in context_window_store
    assert "_project_completed_compaction" not in context_window_store


def test_profile_is_frozen_at_acceptance_and_not_reloaded_during_inference() -> None:
    """@brief Profile 只在 acceptance 读取并随 durable command 固定 / Profile is read only at acceptance and pinned in the durable command."""

    acceptance = (
        SRC_ROOT / "infrastructure" / "database" / "assistant_turn_acceptance.py"
    ).read_text(encoding="utf-8")
    inference = (
        SRC_ROOT / "application" / "assistant" / "durable_inference.py"
    ).read_text(encoding="utf-8")
    assert "read_profile_in_transaction" in acceptance
    assert "read_profile" not in inference
    assert "PostgresUserProfileStore" not in inference


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


def test_assistant_memory_operation_depends_only_on_port_and_dto() -> None:
    """@brief Assistant Memory operation 不依赖 provider 或数据库 / Assistant Memory operation does not depend on provider or database."""

    path = (
        SRC_ROOT / "infrastructure" / "assistant" / "tool_operations" / "memory.py"
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
    assert "scope_kind TEXT NOT NULL" in snapshot
    assert "scope_id BIGINT NOT NULL" in snapshot
    assert "personal_user_id BIGINT NULL" in snapshot
    assert "personal_user_id = scope_id" in snapshot
    retrieval_section = snapshot.split("CREATE TABLE retrieval.source_projections", 1)[1]
    assert "owner_user_id" not in retrieval_section.split(
        "CREATE TABLE user_profile.evidence_events", 1
    )[0]
    assert "CREATE TABLE user_profile.evidence_events" in snapshot
    assert "CREATE TABLE user_profile.profile_revisions" in snapshot
    assert "CREATE TABLE user_profile.dreams" in snapshot
    assert "CREATE TABLE user_profile.dream_sources" in snapshot
    assert "evidence_events(event_id) ON DELETE CASCADE" in snapshot
    assert "CREATE TABLE assistant.ai_user_affection" not in snapshot


def test_forgetting_is_enforced_at_discovery_and_projection_commit() -> None:
    """@brief 遗忘同时约束来源发现与最终投影提交 / Forgetting constrains both source discovery and final projection commit.

    @return None / None.
    @note 边界比较使用 Turn acceptance time；重置前开始但稍后完成的 Turn 仍属于旧状态。/
        The boundary uses Turn acceptance time, so a Turn started before reset but completed
        later still belongs to the forgotten state.
    """

    retrieval = (
        SRC_ROOT / "infrastructure" / "database" / "retrieval.py"
    ).read_text(encoding="utf-8")
    profile_source = (
        SRC_ROOT
        / "infrastructure"
        / "database"
        / "user_profile"
        / "source.py"
    ).read_text(encoding="utf-8")
    profile_store = (
        SRC_ROOT
        / "infrastructure"
        / "database"
        / "user_profile"
        / "store.py"
    ).read_text(encoding="utf-8")

    assert "retrieval.scope_forgetting_boundaries" in retrieval
    assert "turn.created_at <= boundary.forgotten_through" in retrieval
    assert "await lock_retrieval_scope(connection, turn.scope)" in retrieval
    assert "turn.created_at <= profile.forgotten_through" in profile_source
    assert "await lock_user_profile(connection, evidence.owner_user_id)" in profile_store
    assert "await lock_user_profile(connection, claim.owner_user_id)" in profile_store
