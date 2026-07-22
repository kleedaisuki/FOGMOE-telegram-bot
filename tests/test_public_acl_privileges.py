"""@brief PostgreSQL PUBLIC 继承边界契约 / PostgreSQL PUBLIC-inheritance boundary contracts."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.commands import access_sql
from fogmoe_dbctl.commands.access_policy import DEFAULT_ACCESS_POLICY
from fogmoe_dbctl.migrations import runner


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Project root directory."""

_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0065_close_public_user_object_access.sql"
)
"""@brief 0065 PostgreSQL migration / 0065 PostgreSQL migration."""

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0065_close_public_user_object_access.py"
)
"""@brief 0065 Alembic version / 0065 Alembic version."""

_SNAPSHOT_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief 当前 DDL snapshot / Current DDL snapshot."""

_DEFAULT_ACL_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0066_validate_public_default_acl_owners.sql"
)
"""@brief 0066 全 owner default-ACL guard migration / 0066 all-owner default-ACL guard migration."""

_CREATOR_CLOSURE_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0067_close_schema_creator_and_default_gaps.sql"
)
"""@brief 0067 creator/default-presence guard migration / 0067 creator/default-presence guard migration."""

_CREATOR_CLOSURE_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0067_close_schema_creator_and_default_gaps.py"
)
"""@brief 0067 Alembic version / 0067 Alembic version."""


def test_0065_closes_public_user_objects_with_one_trusted_extension_gate() -> None:
    """@brief 0065 收敛所有用户对象并只保留 schema 门控的 vector 例外 / 0065 converges user objects with only the schema-gated vector exception."""

    version = _VERSION_PATH.read_text(encoding="utf-8")
    sections = runner._sections(
        _MIGRATION_PATH.read_text(encoding="utf-8"),
        _MIGRATION_PATH,
    )
    upgrade = sections["up"]
    downgrade = sections["down"]

    assert 'revision = "0065_close_public_user_object_access"' in version
    assert 'down_revision = "0064_lock_down_function_execution"' in version
    for object_kind in (
        "SCHEMA %I",
        "TABLE %I.%I",
        "SEQUENCE %I.%I",
        "ROUTINE %I.%I(%s)",
        "TYPE %I.%I",
        "LARGE OBJECT %s",
    ):
        assert f"REVOKE ALL PRIVILEGES ON {object_kind} FROM PUBLIC" in upgrade
    assert "attribute.attacl" in upgrade
    assert "extension.extname = 'vector'" in upgrade
    assert "GRANT USAGE ON SCHEMA public TO {{ application_role }}" in upgrade
    assert "{{ application_role_literal }}" in upgrade
    assert "REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC" in upgrade
    assert "REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC" in upgrade
    assert "irreversible" in downgrade
    assert "GRANT" not in downgrade


def test_grant_convergence_revokes_public_and_restores_only_application_types() -> None:
    """@brief grant 收敛先移除 PUBLIC/历史类型 ACL，再显式恢复应用依赖 / Grant convergence removes PUBLIC and historical type ACLs before restoring application dependencies."""

    policy = DEFAULT_ACCESS_POLICY
    runtime_sql = access_sql.build_runtime_grant_sql(
        database="fogmoe",
        policy=policy,
        application_role="fogmoe-app",
        owner_role="fogmoe-maintenance",
    )
    reporting_sql = access_sql.build_reporting_grant_sql(
        database="fogmoe",
        policy=policy,
        reporting_role="fogmoe-dashboard",
        owner_role="fogmoe-maintenance",
    )

    for sql in (runtime_sql, reporting_sql):
        assert "fogmoe_public_acl_revoke" in sql
        assert "fogmoe_public_acl_guard" in sql
        assert "REVOKE ALL PRIVILEGES ON SCHEMA %I FROM PUBLIC" in sql
        assert "REVOKE ALL PRIVILEGES ON ROUTINE %I.%I(%s) FROM PUBLIC" in sql
        assert "REVOKE ALL PRIVILEGES ON TYPE %I.%I FROM PUBLIC" in sql
        assert "pg_largeobject_metadata" in sql
        assert "extension.extname IN ('vector')" in sql
        assert "defaclobjtype IN ('r', 'S', 'f', 'T', 'n')" in sql
        assert "current_user <> 'fogmoe-maintenance'" in sql
        assert "candidate_role.rolcanlogin" in sql
        assert "NOT candidate_role.rolsuper" in sql
        assert "has_schema_privilege(" in sql
        assert "candidate_role.rolname <> 'fogmoe-maintenance'" in sql
        assert "(VALUES ('f'::\"char\"), ('T'::\"char\"))" in sql
        assert "owner_role.rolname = 'fogmoe-maintenance'" in sql
        assert "default_acl.defaclnamespace = 0" in sql

    assert "REVOKE USAGE ON SCHEMA public FROM PUBLIC" in runtime_sql
    assert 'GRANT USAGE ON SCHEMA public TO "fogmoe-app"' in runtime_sql
    assert "fogmoe_type_grant" in runtime_sql
    assert 'GRANT USAGE ON TYPES TO "fogmoe-app"' in runtime_sql
    assert 'GRANT USAGE ON SCHEMA public TO "fogmoe-dashboard"' not in reporting_sql


def test_schema_snapshot_records_the_public_boundary_head() -> None:
    """@brief snapshot 记录 0067 head 与完整 ACL 证明 / The snapshot records the 0067 head and complete ACL proof."""

    snapshot = _SNAPSHOT_PATH.read_text(encoding="utf-8")

    assert "-- Alembic head: 0067_close_schema_creator_and_default_gaps" in snapshot
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC" in snapshot
    assert "REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC" in snapshot
    assert "fogmoe_snapshot_public_acl" in snapshot
    assert "fogmoe_snapshot_acl_proof" in snapshot
    assert "candidate_role.rolcanlogin" in snapshot
    assert "has_schema_privilege(" in snapshot
    assert "(VALUES ('f'::\"char\"), ('T'::\"char\"))" in snapshot
    assert "default_acl.defaclnamespace = 0" in snapshot


def test_0066_guards_public_defaults_owned_by_every_role() -> None:
    """@brief 0066 不允许第三方 owner 以 default ACL 重开 PUBLIC / 0066 prevents any third-party owner from reopening PUBLIC via default ACLs."""

    upgrade = runner._sections(
        _DEFAULT_ACL_MIGRATION_PATH.read_text(encoding="utf-8"),
        _DEFAULT_ACL_MIGRATION_PATH,
    )["up"]

    assert "defaclrole" not in upgrade
    assert "defaclobjtype IN ('r', 'S', 'f', 'T', 'n')" in upgrade
    assert "default_acl.defaclnamespace = 0" in upgrade
    assert "namespace.nspname !~ '^pg_'" in upgrade
    assert "privilege.grantee = 0" in upgrade


def test_0067_proves_schema_creator_and_owner_default_closure() -> None:
    """@brief 0067 证明 creator 唯一性并排除缺行恢复内建 PUBLIC 默认 / 0067 proves creator exclusivity and excludes missing-row built-in PUBLIC defaults."""

    version = _CREATOR_CLOSURE_VERSION_PATH.read_text(encoding="utf-8")
    upgrade = runner._sections(
        _CREATOR_CLOSURE_MIGRATION_PATH.read_text(encoding="utf-8"),
        _CREATOR_CLOSURE_MIGRATION_PATH,
    )["up"]

    assert 'revision = "0067_close_schema_creator_and_default_gaps"' in version
    assert 'down_revision = "0066_validate_public_default_acl_owners"' in version
    assert "migration_role.rolname = current_user" in upgrade
    assert "migration_role.rolcanlogin" in upgrade
    assert "NOT migration_role.rolsuper" in upgrade
    assert "candidate_role.rolname <> current_user" in upgrade
    assert "candidate_role.rolcanlogin" in upgrade
    assert "NOT candidate_role.rolsuper" in upgrade
    assert "has_schema_privilege(" in upgrade
    assert "(VALUES ('f'::\"char\"), ('T'::\"char\"))" in upgrade
    assert "owner_role.rolname = current_user" in upgrade
    assert "default_acl.defaclnamespace = 0" in upgrade
    assert "privilege.grantee = 0" in upgrade
