"""@brief PostgreSQL routine 执行权限边界契约 / PostgreSQL routine-execution privilege-boundary contracts."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""

_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0064_lock_down_function_execution.sql"
)
"""@brief 0064 PostgreSQL migration / 0064 PostgreSQL migration."""

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0064_lock_down_function_execution.py"
)
"""@brief 0064 Alembic version / 0064 Alembic version."""

_SNAPSHOT_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief 当前 DDL snapshot / Current DDL snapshot."""


def test_0064_revokes_public_before_allowing_only_the_application_role() -> None:
    """@brief 0064 在同一迁移事务收回 PUBLIC 并显式放行应用角色 / 0064 revokes PUBLIC and explicitly allows the application role in one migration transaction.

    @return None / None.
    """

    version = _VERSION_PATH.read_text(encoding="utf-8")
    sections = runner._sections(
        _MIGRATION_PATH.read_text(encoding="utf-8"),
        _MIGRATION_PATH,
    )
    upgrade = sections["up"]

    assert 'revision = "0064_lock_down_function_execution"' in version
    assert 'down_revision = "0063_observability_resource_liveness"' in version
    assert "SET LOCAL lock_timeout = '5s'" in upgrade
    assert "SET LOCAL statement_timeout = '30s'" in upgrade
    assert upgrade.count("FROM PUBLIC") == 4
    assert "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA %I FROM PUBLIC" in upgrade
    assert upgrade.count("TO {{ application_role }}") == 2
    assert "REVOKE EXECUTE ON ROUTINES FROM PUBLIC" in upgrade
    for routine in (
        "observability.ensure_daily_partitions(DATE)",
        "observability.drop_partitions_before(DATE)",
    ):
        revoke = upgrade.index(f"ON FUNCTION {routine}")
        grant = upgrade.index(f"ON FUNCTION {routine}", revoke + 1)
        assert revoke < grant


def test_schema_snapshot_never_exposes_security_definer_routines_to_public() -> None:
    """@brief snapshot 的特权函数与未来 routine 默认权限均不开放给 PUBLIC / The snapshot exposes neither privileged routines nor future routine defaults to PUBLIC.

    @return None / None.
    """

    snapshot = _SNAPSHOT_PATH.read_text(encoding="utf-8")

    assert "-- Alembic head: 0067_close_schema_creator_and_default_gaps" in snapshot
    assert (
        "ALTER DEFAULT PRIVILEGES\n  REVOKE EXECUTE ON ROUTINES FROM PUBLIC" in snapshot
    )
    assert snapshot.count("SECURITY DEFINER") == 2
    for routine in (
        "observability.ensure_daily_partitions(DATE)",
        "observability.drop_partitions_before(DATE)",
    ):
        assert (
            f"REVOKE ALL PRIVILEGES\n  ON FUNCTION {routine}\n  FROM PUBLIC" in snapshot
        )


def test_0064_exposes_cross_context_queues_only_as_aggregate_read_models() -> None:
    """@brief 0064 以安全聚合 view 隐藏检索向量与画像原始内容 / 0064 hides raw retrieval vectors and profile content behind secure aggregate views.

    @return None / None.
    """

    sections = runner._sections(
        _MIGRATION_PATH.read_text(encoding="utf-8"),
        _MIGRATION_PATH,
    )
    upgrade = sections["up"]

    assert (
        "CREATE OR REPLACE VIEW observability.pipeline_health\n"
        "WITH (security_barrier = true, security_invoker = false)"
    ) in upgrade
    assert (
        "CREATE VIEW observability.retrieval_queue_health\n"
        "WITH (security_barrier = true, security_invoker = false)"
    ) in upgrade
    assert "FROM retrieval.passage_vectors" in upgrade
    assert "FROM user_profile.dreams" in upgrade
    for private_expression in (
        "vector.embedding",
        "vector.last_error",
        "result_patch",
        "dream.user_id",
    ):
        assert private_expression not in upgrade


def test_dashboard_queries_only_the_observability_read_models() -> None:
    """@brief Dashboard 不再跨层读取 Retrieval 与 User Profile 存储 / Dashboard no longer crosses layers into Retrieval and User Profile storage.

    @return None / None.
    """

    repository = (
        _PROJECT_ROOT / "src/fogmoe_dashboard/infrastructure/postgres.py"
    ).read_text(encoding="utf-8")

    assert "FROM observability.pipeline_health" in repository
    assert "FROM observability.retrieval_queue_health" in repository
    assert "FROM retrieval.passage_vectors" not in repository
    assert "FROM retrieval.embedding_spaces" not in repository
    assert "FROM user_profile.dreams" not in repository
