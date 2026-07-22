"""@brief Alembic 数据库迁移子命令 / Alembic database migration subcommand."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from fogmoe_dbctl.config import DbctlSettings, reveal_secret
from fogmoe_dbctl.postgres import (
    direct_psql_environment,
    dollar_quote,
    quote_identifier,
    quote_literal,
    sqlalchemy_url,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
"""@brief 仓库根目录 / Repository root directory."""

_APPLICATION_SCHEMAS = (
    "identity",
    "conversation",
    "context_window",
    "retrieval",
    "user_profile",
    "assistant",
    "scheduling",
    "economy",
    "moderation",
    "crypto",
    "game",
    "media",
    "admin",
    "observability",
    "bank",
    "billing",
    "town",
    "chance",
    "personal_rpg",
)
"""@brief 迁移拥有且应用需要访问的 schema / Schemas owned by migrations and used by the application."""

_APPLICATION_FUNCTIONS = (
    ("observability", "ensure_daily_partitions", "DATE"),
    ("observability", "drop_partitions_before", "DATE"),
)
"""@brief 运行时可直接调用的函数闭集 / Closed set of functions directly callable by the runtime."""

_TRUSTED_PUBLIC_EXTENSIONS = ("vector",)
"""@brief 通过私有 schema USAGE 门控的受信 public 扩展 / Trusted public extensions gated by private schema USAGE."""

_REPORTING_RELATIONS = (
    (
        "observability",
        (
            "resources",
            "log_records",
            "spans",
            "metric_points",
            "pipeline_health",
            "turn_latency",
            "retrieval_queue_health",
        ),
    ),
)
"""@brief Dashboard 查询所需观测读模型的显式 allow-list / Explicit allow-list of observability read models queried by Dashboard."""


def configure_parser(subparsers: Any) -> None:
    """@brief 注册迁移子命令 / Register the migration subcommand.

    @param subparsers argparse 子命令集合 / argparse subparser collection.
    @return None / None.
    """

    parser = subparsers.add_parser(
        "migrate",
        help="Run Alembic migrations and grant runtime/reporting access.",
        description=(
            "Upgrade the configured database with the maintenance role, then grant "
            "the application role runtime privileges and the reporting role read-only "
            "privileges."
        ),
    )
    parser.add_argument("--revision", default="head")
    parser.add_argument(
        "--skip-grants",
        action="store_true",
        help="Run migrations without granting application or reporting privileges.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print operations without changing the database.",
    )
    parser.set_defaults(handler=execute)


def maintenance_database_url(settings: DbctlSettings) -> str:
    """@brief 构造维护角色的 SQLAlchemy URL / Build the maintenance-role SQLAlchemy URL.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @return asyncpg SQLAlchemy URL / asyncpg SQLAlchemy URL.
    """

    return sqlalchemy_url(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )


def run_alembic(
    *,
    settings: DbctlSettings,
    revision: str,
    dry_run: bool,
) -> None:
    """@brief 通过程序化 API 执行 Alembic / Run Alembic through its programmatic API.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param revision Alembic 目标 revision / Alembic target revision.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    @note 所有迁移输入显式写入 Alembic attributes，迁移环境不读取环境变量。/
        All migration inputs are injected into Alembic attributes; the migration environment reads no environment variables.
    """

    if dry_run:
        print(f"alembic upgrade {revision}")
        return

    alembic_config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    alembic_config.attributes["database_url"] = maintenance_database_url(settings)
    alembic_config.attributes["migration_schema"] = (
        settings.maintenance.migration_schema
    )
    alembic_config.attributes["admin_user_id"] = settings.administrator.user_id
    alembic_config.attributes["application_role"] = settings.application.username
    command.upgrade(alembic_config, revision)


def _build_role_object_revoke_sql(role: str) -> str:
    """@brief 撤销一个受管角色在全部用户 schema 的历史对象 ACL / Revoke a managed role's historical object ACLs across all user schemas.

    @param role 待收敛角色名 / Role to converge.
    @return 可在授权事务中执行的 PL/pgSQL / PL/pgSQL executable in the grant transaction.
    @note 动态枚举避免未知或 ``public`` schema 成为 allow-list 后门；若 maintenance
        不能撤销第三方 owner 授予的权限，后续 guard 会失败关闭。/ Dynamic enumeration
        prevents unknown or ``public`` schemas from bypassing the allow-list. If maintenance
        cannot revoke privileges granted by a third-party owner, the subsequent guard fails closed.
    """

    role_literal = quote_literal(role)
    body = f"""
DECLARE
  target_schema RECORD;
  target_column RECORD;
  target_routine RECORD;
  target_type RECORD;
BEGIN
  FOR target_schema IN
    SELECT namespace.nspname AS schema_name
    FROM pg_namespace AS namespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON SCHEMA %I FROM %I',
      target_schema.schema_name,
      {role_literal}
    );
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I',
      target_schema.schema_name,
      {role_literal}
    );
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM %I',
      target_schema.schema_name,
      {role_literal}
    );
  END LOOP;

  FOR target_routine IN
    SELECT DISTINCT namespace.nspname AS schema_name,
           routine.proname AS routine_name,
           pg_get_function_identity_arguments(routine.oid) AS arguments
    FROM pg_proc AS routine
    JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
    CROSS JOIN LATERAL aclexplode(routine.proacl) AS privilege
    JOIN pg_roles AS grantee_role ON grantee_role.oid = privilege.grantee
    WHERE grantee_role.rolname = {role_literal}
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON ROUTINE %I.%I(%s) FROM %I CASCADE',
      target_routine.schema_name,
      target_routine.routine_name,
      target_routine.arguments,
      {role_literal}
    );
  END LOOP;

  FOR target_column IN
    SELECT DISTINCT namespace.nspname AS schema_name,
           relation.relname AS relation_name,
           attribute.attname AS column_name
    FROM pg_attribute AS attribute
    JOIN pg_class AS relation ON relation.oid = attribute.attrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
    JOIN pg_roles AS grantee_role ON grantee_role.oid = privilege.grantee
    WHERE attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND grantee_role.rolname = {role_literal}
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM %I CASCADE',
      target_column.column_name,
      target_column.schema_name,
      target_column.relation_name,
      {role_literal}
    );
  END LOOP;

  FOR target_type IN
    SELECT DISTINCT namespace.nspname AS schema_name,
           data_type.typname AS type_name
    FROM pg_type AS data_type
    JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
    CROSS JOIN LATERAL aclexplode(data_type.typacl) AS privilege
    JOIN pg_roles AS grantee_role ON grantee_role.oid = privilege.grantee
    WHERE grantee_role.rolname = {role_literal}
      AND NOT EXISTS (
        SELECT 1
        FROM pg_type AS element_type
        WHERE element_type.typarray = data_type.oid
      )
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON TYPE %I.%I FROM %I CASCADE',
      target_type.schema_name,
      target_type.type_name,
      {role_literal}
    );
  END LOOP;
END;
""".strip()
    return f"DO {dollar_quote(body, prefix='fogmoe_acl_revoke')};\n"


def _build_public_object_revoke_sql(owner_role: str) -> str:
    """@brief 收敛所有用户 schema 中可被任意登录继承的 PUBLIC ACL / Converge PUBLIC ACLs inherited by every login across all user schemas.

    @param owner_role 迁移对象 owner 角色名 / Migration-object owner role name.
    @return PUBLIC 对象与默认权限撤权 SQL / PUBLIC object and default-privilege revocation SQL.
    @note ``vector`` 由 bootstrap superuser 拥有，maintenance 不能修改其成员 ACL；
        这些成员保留扩展自带权限，但 ``public`` schema 的 PUBLIC USAGE 被撤销，只有
        application 获得直接 USAGE。/ ``vector`` members are owned by the bootstrap
        superuser and cannot be re-ACL'd by maintenance. Their extension ACLs remain, while
        PUBLIC loses USAGE on the ``public`` schema and only the application receives it directly.
    """

    owner_literal = quote_literal(owner_role)
    trusted_extensions = ", ".join(
        quote_literal(extension) for extension in _TRUSTED_PUBLIC_EXTENSIONS
    )
    body = f"""
DECLARE
  target_schema RECORD;
  target_relation RECORD;
  target_column RECORD;
  target_routine RECORD;
  target_type RECORD;
  target_large_object RECORD;
BEGIN
  FOR target_schema IN
    SELECT namespace.nspname AS schema_name
    FROM pg_namespace AS namespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON SCHEMA %I FROM PUBLIC',
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC',
      {owner_literal},
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC',
      {owner_literal},
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC',
      {owner_literal},
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC',
      {owner_literal},
      target_schema.schema_name
    );
  END LOOP;

  FOR target_relation IN
    SELECT namespace.nspname AS schema_name,
           relation.relname AS relation_name,
           relation.relkind AS relation_kind
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
      AND NOT (
        namespace.nspname = 'public'
        AND EXISTS (
          SELECT 1
          FROM pg_depend AS dependency
          JOIN pg_extension AS extension
            ON extension.oid = dependency.refobjid
          WHERE dependency.classid = 'pg_class'::REGCLASS
            AND dependency.objid = relation.oid
            AND dependency.objsubid = 0
            AND dependency.refclassid = 'pg_extension'::REGCLASS
            AND dependency.deptype = 'e'
            AND extension.extname IN ({trusted_extensions})
        )
      )
  LOOP
    IF target_relation.relation_kind = 'S' THEN
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON SEQUENCE %I.%I FROM PUBLIC CASCADE',
        target_relation.schema_name,
        target_relation.relation_name
      );
    ELSE
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM PUBLIC CASCADE',
        target_relation.schema_name,
        target_relation.relation_name
      );
    END IF;
  END LOOP;

  FOR target_column IN
    SELECT DISTINCT namespace.nspname AS schema_name,
           relation.relname AS relation_name,
           attribute.attname AS column_name
    FROM pg_attribute AS attribute
    JOIN pg_class AS relation ON relation.oid = attribute.attrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND privilege.grantee = 0
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES (%I) ON TABLE %I.%I FROM PUBLIC CASCADE',
      target_column.column_name,
      target_column.schema_name,
      target_column.relation_name
    );
  END LOOP;

  FOR target_routine IN
    SELECT namespace.nspname AS schema_name,
           routine.proname AS routine_name,
           pg_get_function_identity_arguments(routine.oid) AS arguments
    FROM pg_proc AS routine
    JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND NOT (
        namespace.nspname = 'public'
        AND EXISTS (
          SELECT 1
          FROM pg_depend AS dependency
          JOIN pg_extension AS extension
            ON extension.oid = dependency.refobjid
          WHERE dependency.classid = 'pg_proc'::REGCLASS
            AND dependency.objid = routine.oid
            AND dependency.objsubid = 0
            AND dependency.refclassid = 'pg_extension'::REGCLASS
            AND dependency.deptype = 'e'
            AND extension.extname IN ({trusted_extensions})
        )
      )
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON ROUTINE %I.%I(%s) FROM PUBLIC CASCADE',
      target_routine.schema_name,
      target_routine.routine_name,
      target_routine.arguments
    );
  END LOOP;

  FOR target_type IN
    SELECT namespace.nspname AS schema_name,
           data_type.typname AS type_name
    FROM pg_type AS data_type
    JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND NOT EXISTS (
        SELECT 1
        FROM pg_type AS element_type
        WHERE element_type.typarray = data_type.oid
      )
      AND NOT (
        namespace.nspname = 'public'
        AND (
          EXISTS (
            SELECT 1
            FROM pg_depend AS dependency
            JOIN pg_extension AS extension
              ON extension.oid = dependency.refobjid
            WHERE dependency.classid = 'pg_type'::REGCLASS
              AND dependency.objid = data_type.oid
              AND dependency.objsubid = 0
              AND dependency.refclassid = 'pg_extension'::REGCLASS
              AND dependency.deptype = 'e'
              AND extension.extname IN ({trusted_extensions})
          )
          OR EXISTS (
            SELECT 1
            FROM pg_depend AS dependency
            JOIN pg_extension AS extension
              ON extension.oid = dependency.refobjid
            WHERE dependency.classid = 'pg_class'::REGCLASS
              AND dependency.objid = data_type.typrelid
              AND dependency.objsubid = 0
              AND dependency.refclassid = 'pg_extension'::REGCLASS
              AND dependency.deptype = 'e'
              AND extension.extname IN ({trusted_extensions})
          )
        )
      )
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON TYPE %I.%I FROM PUBLIC CASCADE',
      target_type.schema_name,
      target_type.type_name
    );
  END LOOP;

  FOR target_large_object IN
    SELECT DISTINCT metadata.oid AS large_object_oid
    FROM pg_largeobject_metadata AS metadata
    CROSS JOIN LATERAL aclexplode(metadata.lomacl) AS privilege
    WHERE privilege.grantee = 0
  LOOP
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON LARGE OBJECT %s FROM PUBLIC CASCADE',
      target_large_object.large_object_oid
    );
  END LOOP;
END;
""".strip()
    default_revocations = "\n".join(
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_identifier(owner_role)} "
            "REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC;",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_identifier(owner_role)} "
            "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_identifier(owner_role)} "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_identifier(owner_role)} "
            "REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC;",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote_identifier(owner_role)} "
            "REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC;",
        )
    )
    return (
        f"DO {dollar_quote(body, prefix='fogmoe_public_acl_revoke')};\n"
        f"{default_revocations}\n"
    )


def _build_public_acl_guard_sql(owner_role: str) -> str:
    """@brief 验证 PUBLIC 与 schema 创建权的完整边界 / Verify the complete PUBLIC and schema-creation boundary.

    @param owner_role 预期执行收敛的 maintenance owner / Expected maintenance owner executing convergence.
    @return 带受信 vector 例外的失败关闭 guard / Fail-closed guard with a trusted-vector exception.
    @note 函数与类型的 PostgreSQL 内建默认会授权 PUBLIC；因此除了
        检查现有 default ACL，还必须证明 owner 的全局 ``f``/``T`` 覆盖行
        显式存在。/ PostgreSQL's built-in function and type defaults grant PUBLIC;
        therefore the owner's global ``f``/``T`` override rows must explicitly exist in
        addition to checking existing default ACLs.
    """

    owner_literal = quote_literal(owner_role)
    trusted_extensions = ", ".join(
        quote_literal(extension) for extension in _TRUSTED_PUBLIC_EXTENSIONS
    )
    body = f"""
BEGIN
  IF current_user <> {owner_literal} THEN
    RAISE EXCEPTION
      'grant convergence must run as the configured maintenance owner: %',
      {owner_literal}
      USING ERRCODE = '42501';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_namespace AS namespace
    CROSS JOIN pg_roles AS candidate_role
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND candidate_role.rolcanlogin
      AND NOT candidate_role.rolsuper
      AND candidate_role.rolname <> {owner_literal}
      AND has_schema_privilege(
        candidate_role.oid,
        namespace.oid,
        'CREATE'
      )
  ) THEN
    RAISE EXCEPTION
      'a non-superuser login other than maintenance can CREATE in a non-system schema'
      USING ERRCODE = '42501';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM (VALUES ('f'::"char"), ('T'::"char")) AS required_acl(object_type)
    WHERE NOT EXISTS (
      SELECT 1
      FROM pg_default_acl AS default_acl
      JOIN pg_roles AS owner_role
        ON owner_role.oid = default_acl.defaclrole
      WHERE owner_role.rolname = {owner_literal}
        AND default_acl.defaclnamespace = 0
        AND default_acl.defaclobjtype = required_acl.object_type
        AND NOT EXISTS (
          SELECT 1
          FROM aclexplode(default_acl.defaclacl) AS privilege
          WHERE privilege.grantee = 0
        )
    )
  ) THEN
    RAISE EXCEPTION
      'maintenance requires explicit global routine and type default ACLs without PUBLIC'
      USING ERRCODE = '42501';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_namespace AS namespace
    CROSS JOIN LATERAL aclexplode(
      COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
    ) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND privilege.grantee = 0
  ) OR EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL aclexplode(relation.relacl) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND privilege.grantee = 0
      AND NOT (
        namespace.nspname = 'public'
        AND EXISTS (
          SELECT 1
          FROM pg_depend AS dependency
          JOIN pg_extension AS extension
            ON extension.oid = dependency.refobjid
          WHERE dependency.classid = 'pg_class'::REGCLASS
            AND dependency.objid = relation.oid
            AND dependency.objsubid = 0
            AND dependency.refclassid = 'pg_extension'::REGCLASS
            AND dependency.deptype = 'e'
            AND extension.extname IN ({trusted_extensions})
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_attribute AS attribute
    JOIN pg_class AS relation ON relation.oid = attribute.attrelid
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND privilege.grantee = 0
  ) OR EXISTS (
    SELECT 1
    FROM pg_proc AS routine
    JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
    CROSS JOIN LATERAL aclexplode(
      COALESCE(routine.proacl, acldefault('f', routine.proowner))
    ) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND privilege.grantee = 0
      AND NOT (
        namespace.nspname = 'public'
        AND EXISTS (
          SELECT 1
          FROM pg_depend AS dependency
          JOIN pg_extension AS extension
            ON extension.oid = dependency.refobjid
          WHERE dependency.classid = 'pg_proc'::REGCLASS
            AND dependency.objid = routine.oid
            AND dependency.objsubid = 0
            AND dependency.refclassid = 'pg_extension'::REGCLASS
            AND dependency.deptype = 'e'
            AND extension.extname IN ({trusted_extensions})
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_largeobject_metadata AS metadata
    CROSS JOIN LATERAL aclexplode(metadata.lomacl) AS privilege
    WHERE privilege.grantee = 0
  ) OR EXISTS (
    SELECT 1
    FROM pg_type AS data_type
    JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
    CROSS JOIN LATERAL aclexplode(
      COALESCE(data_type.typacl, acldefault('T', data_type.typowner))
    ) AS privilege
    WHERE namespace.nspname <> 'information_schema'
      AND namespace.nspname !~ '^pg_'
      AND privilege.grantee = 0
      AND NOT EXISTS (
        SELECT 1
        FROM pg_type AS element_type
        WHERE element_type.typarray = data_type.oid
      )
      AND NOT (
        namespace.nspname = 'public'
        AND (
          EXISTS (
            SELECT 1
            FROM pg_depend AS dependency
            JOIN pg_extension AS extension
              ON extension.oid = dependency.refobjid
            WHERE dependency.classid = 'pg_type'::REGCLASS
              AND dependency.objid = data_type.oid
              AND dependency.objsubid = 0
              AND dependency.refclassid = 'pg_extension'::REGCLASS
              AND dependency.deptype = 'e'
              AND extension.extname IN ({trusted_extensions})
          )
          OR EXISTS (
            SELECT 1
            FROM pg_depend AS dependency
            JOIN pg_extension AS extension
              ON extension.oid = dependency.refobjid
            WHERE dependency.classid = 'pg_class'::REGCLASS
              AND dependency.objid = data_type.typrelid
              AND dependency.objsubid = 0
              AND dependency.refclassid = 'pg_extension'::REGCLASS
              AND dependency.deptype = 'e'
              AND extension.extname IN ({trusted_extensions})
          )
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_default_acl AS default_acl
    LEFT JOIN pg_namespace AS namespace
      ON namespace.oid = default_acl.defaclnamespace
    CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) AS privilege
    WHERE default_acl.defaclobjtype IN ('r', 'S', 'f', 'T', 'n')
      AND privilege.grantee = 0
      AND (
        default_acl.defaclnamespace = 0
        OR (
          namespace.nspname <> 'information_schema'
          AND namespace.nspname !~ '^pg_'
        )
      )
  ) THEN
    RAISE EXCEPTION
      'PUBLIC retains access to a non-system user object outside the trusted extension boundary'
      USING ERRCODE = '42501';
  END IF;
END;
""".strip()
    return f"DO {dollar_quote(body, prefix='fogmoe_public_acl_guard')};\n"


def _build_role_type_grant_sql(role: str, schemas: tuple[str, ...]) -> str:
    """@brief 向应用显式授予当前业务类型及复合行类型 / Explicitly grant current business and composite row types to the application.

    @param role 接收类型 USAGE 的角色 / Role receiving type USAGE.
    @param schemas 允许类型依赖的业务 schema / Business schemas whose types are allowed.
    @return 排除自动数组类型的动态授权 SQL / Dynamic grant SQL excluding automatic array types.
    @raise ValueError schema 闭集为空时抛出 / Raised when the schema set is empty.
    @note PostgreSQL 不允许直接修改自动数组类型 ACL；其 USAGE 跟随 element type。/
        PostgreSQL does not permit direct ACL changes on automatic array types; their USAGE
        follows the element type.
    """

    if not schemas:
        raise ValueError("Role type-grant schema allow-list cannot be empty")
    role_literal = quote_literal(role)
    schema_literals = ", ".join(quote_literal(schema) for schema in schemas)
    body = f"""
DECLARE
  target_type RECORD;
BEGIN
  FOR target_type IN
    SELECT namespace.nspname AS schema_name,
           data_type.typname AS type_name
    FROM pg_type AS data_type
    JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
    WHERE namespace.nspname IN ({schema_literals})
      AND NOT EXISTS (
        SELECT 1
        FROM pg_type AS element_type
        WHERE element_type.typarray = data_type.oid
      )
  LOOP
    EXECUTE format(
      'GRANT USAGE ON TYPE %I.%I TO %I',
      target_type.schema_name,
      target_type.type_name,
      {role_literal}
    );
  END LOOP;
END;
""".strip()
    return f"DO {dollar_quote(body, prefix='fogmoe_type_grant')};\n"


def _build_role_acl_guard_sql(role: str) -> str:
    """@brief 验证撤权后没有任何 catalog 类别的残留 ACL / Verify that no ACL remains in any catalog category after revocation.

    @param role 待验证角色名 / Role to verify.
    @return 失败关闭 guard SQL / Fail-closed guard SQL.
    """

    role_literal = quote_literal(role)
    body = f"""
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_shdepend AS dependency
    JOIN pg_roles AS grantee ON grantee.oid = dependency.refobjid
    WHERE dependency.deptype = 'a'
      AND grantee.rolname = {role_literal}
  ) THEN
    RAISE EXCEPTION
      'managed role retains an ACL outside the declared allow-list: %',
      {role_literal}
      USING ERRCODE = '42501';
  END IF;
END;
""".strip()
    return f"DO {dollar_quote(body, prefix='fogmoe_acl_guard')};\n"


def build_runtime_grant_sql(
    *,
    database: str,
    schemas: tuple[str, ...],
    functions: tuple[tuple[str, str, str], ...],
    application_role: str,
    owner_role: str,
) -> str:
    """@brief 构造运行时授权 SQL / Build runtime grant SQL.

    @param database 应用数据库名 / Application database name.
    @param schemas 应用 schema 列表 / Application schema list.
    @param functions 应用可调用的 schema、函数名与参数签名 / Application-callable schema, function name, and argument signature.
    @param application_role 应用角色名 / Application role name.
    @param owner_role 对象 owner 角色名 / Object owner role name.
    @return 可执行 SQL / Executable SQL.
    """

    application_ident = quote_identifier(application_role)
    owner_ident = quote_identifier(owner_role)
    database_ident = quote_identifier(database)
    revocations: list[str] = [
        _build_public_object_revoke_sql(owner_role).rstrip(),
        _build_role_object_revoke_sql(application_role).rstrip(),
        f"REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM PUBLIC;",
        f"REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {application_ident};",
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
        "REVOKE USAGE ON SCHEMA public FROM PUBLIC;",
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON TABLES FROM {application_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {application_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            "REVOKE EXECUTE ON ROUTINES FROM PUBLIC;"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE EXECUTE ON ROUTINES FROM {application_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON TYPES FROM {application_ident};"
        ),
    ]
    for schema in schemas:
        schema_ident = quote_identifier(schema)
        revocations.extend(
            [
                (f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_ident} FROM PUBLIC;"),
                (
                    f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_ident} "
                    f"FROM {application_ident};"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema_ident} "
                    "FROM PUBLIC;"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema_ident} "
                    f"FROM {application_ident};"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema_ident} "
                    "FROM PUBLIC;"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema_ident} "
                    f"FROM {application_ident};"
                ),
                (
                    f"REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA {schema_ident} "
                    "FROM PUBLIC;"
                ),
                (
                    f"REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA {schema_ident} "
                    f"FROM {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"REVOKE ALL PRIVILEGES ON TABLES FROM {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    "REVOKE EXECUTE ON ROUTINES FROM PUBLIC;"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"REVOKE EXECUTE ON ROUTINES FROM {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"REVOKE ALL PRIVILEGES ON TYPES FROM {application_ident};"
                ),
            ]
        )
    grants: list[str] = [
        _build_public_acl_guard_sql(owner_role).rstrip(),
        _build_role_acl_guard_sql(application_role).rstrip(),
        (
            f"GRANT CONNECT, TEMPORARY ON DATABASE {database_ident} "
            f"TO {application_ident};"
        ),
        f"GRANT USAGE ON SCHEMA public TO {application_ident};",
    ]
    for schema in schemas:
        schema_ident = quote_identifier(schema)
        grants.extend(
            [
                f"GRANT USAGE ON SCHEMA {schema_ident} TO {application_ident};",
                (
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA {schema_ident} TO {application_ident};"
                ),
                (
                    "GRANT USAGE, SELECT, UPDATE "
                    f"ON ALL SEQUENCES IN SCHEMA {schema_ident} TO {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {application_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} "
                    f"GRANT USAGE ON TYPES TO {application_ident};"
                ),
            ]
        )
    grants.append(_build_role_type_grant_sql(application_role, schemas).rstrip())
    for schema, function, argument_signature in functions:
        if schema not in schemas:
            raise ValueError(
                f"Runtime function schema is not application-owned: {schema}"
            )
        if not argument_signature or ";" in argument_signature:
            raise ValueError(
                f"Runtime function argument signature is invalid: {function}"
            )
        grants.append(
            "GRANT EXECUTE ON FUNCTION "
            f"{quote_identifier(schema)}.{quote_identifier(function)}"
            f"({argument_signature}) TO {application_ident};"
        )
    return "\n".join((*revocations, *grants)) + "\n"


def build_reporting_grant_sql(
    *,
    database: str,
    owned_schemas: tuple[str, ...],
    relations: tuple[tuple[str, tuple[str, ...]], ...],
    reporting_role: str,
    owner_role: str,
) -> str:
    """@brief 构造报表角色的严格只读授权 SQL / Build strict read-only grants for the reporting role.

    @param database 应用数据库名 / Application database name.
    @param owned_schemas 必须先收回旧权限的应用 schema / Application schemas whose old privileges must first be revoked.
    @param relations Dashboard 可读取关系的 schema 分组 allow-list /
        Schema-grouped allow-list of relations Dashboard may read.
    @param reporting_role 只读报表角色名 / Read-only reporting role name.
    @param owner_role 对象 owner 角色名 / Object owner role name.
    @return 可执行 SQL / Executable SQL.
    @note 先从全部应用 schema 撤销历史授权，再只授予数据库 CONNECT、allow-list
        schema USAGE 与具体关系 SELECT。未来对象没有默认读取权，新增 Dashboard 查询
        必须显式扩充 allow-list。/ Historical grants are first revoked from every
        application schema, then only database CONNECT, allow-listed schema USAGE, and SELECT
        on named relations are granted. Future objects receive no default read access, so a
        new Dashboard query must explicitly expand the allow-list.
    """

    database_ident = quote_identifier(database)
    reporting_ident = quote_identifier(reporting_role)
    owner_ident = quote_identifier(owner_role)
    revocations = [
        _build_public_object_revoke_sql(owner_role).rstrip(),
        _build_role_object_revoke_sql(reporting_role).rstrip(),
        (f"REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {reporting_ident};"),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON TABLES FROM {reporting_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {reporting_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            "REVOKE EXECUTE ON ROUTINES FROM PUBLIC;"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON ROUTINES FROM {reporting_ident};"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
            f"REVOKE ALL PRIVILEGES ON TYPES FROM {reporting_ident};"
        ),
    ]
    for schema in owned_schemas:
        schema_ident = quote_identifier(schema)
        revocations.extend(
            [
                (
                    f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_ident} "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema_ident} "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema_ident} "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA {schema_ident} "
                    "FROM PUBLIC;"
                ),
                (
                    f"REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA {schema_ident} "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} REVOKE ALL PRIVILEGES ON TABLES "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} REVOKE ALL PRIVILEGES ON SEQUENCES "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} REVOKE ALL PRIVILEGES ON ROUTINES "
                    f"FROM {reporting_ident};"
                ),
                (
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} "
                    f"IN SCHEMA {schema_ident} REVOKE ALL PRIVILEGES ON TYPES "
                    f"FROM {reporting_ident};"
                ),
            ]
        )
    grants = [
        _build_public_acl_guard_sql(owner_role).rstrip(),
        _build_role_acl_guard_sql(reporting_role).rstrip(),
        f"GRANT CONNECT ON DATABASE {database_ident} TO {reporting_ident};",
    ]
    for schema, schema_relations in relations:
        if schema not in owned_schemas:
            raise ValueError(
                f"Reporting relation schema is not application-owned: {schema}"
            )
        if not schema_relations:
            raise ValueError(f"Reporting relation allow-list is empty: {schema}")
        schema_ident = quote_identifier(schema)
        relation_idents = ", ".join(
            f"{schema_ident}.{quote_identifier(relation)}"
            for relation in schema_relations
        )
        grants.extend(
            [
                f"GRANT USAGE ON SCHEMA {schema_ident} TO {reporting_ident};",
                f"GRANT SELECT ON TABLE {relation_idents} TO {reporting_ident};",
            ]
        )
    return "\n".join((*revocations, *grants)) + "\n"


def run_psql_grants(
    *,
    settings: DbctlSettings,
    sql: str,
    dry_run: bool,
) -> None:
    """@brief 用 psql 执行运行时授权 / Run runtime grants through psql.

    @param settings dbctl 配置投影 / dbctl configuration projection.
    @param sql SQL 文本 / SQL text.
    @param dry_run 是否只打印 / Whether to print only.
    @return None / None.
    """

    command_line = [
        "psql",
        "--no-psqlrc",
        "--single-transaction",
        "--set",
        "ON_ERROR_STOP=1",
    ]
    if dry_run:
        print("psql --single-transaction --set ON_ERROR_STOP=1")
        print(sql)
        return
    environment = direct_psql_environment(
        host=settings.endpoint.host,
        port=settings.endpoint.port,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=reveal_secret(
            settings.maintenance.password,
            field_name="database.maintenance.password",
        ),
    )
    subprocess.run(command_line, input=sql, text=True, env=environment, check=True)


def execute(args: argparse.Namespace, *, settings: DbctlSettings) -> None:
    """@brief 执行迁移用例 / Execute the migration use case.

    @param args CLI 参数 / CLI arguments.
    @param settings CLI 组合根注入的已验证配置 / Validated settings injected by the CLI composition root.
    @return None / None.
    @note 命令层绝不读取配置文件；配置只在 CLI 根入口读取一次。/
        The command layer never reads a configuration file; configuration is read once at the CLI root.
    """

    if args.revision != "head" and not args.skip_grants:
        raise ValueError(
            "Non-head migrations require --skip-grants because the head allow-list "
            "may reference relations absent from the target revision"
        )

    run_alembic(
        settings=settings,
        revision=args.revision,
        dry_run=args.dry_run,
    )
    if args.skip_grants:
        return
    grant_sql = build_runtime_grant_sql(
        database=settings.endpoint.name,
        schemas=_APPLICATION_SCHEMAS,
        functions=_APPLICATION_FUNCTIONS,
        application_role=settings.application.username,
        owner_role=settings.maintenance.username,
    ) + build_reporting_grant_sql(
        database=settings.endpoint.name,
        owned_schemas=_APPLICATION_SCHEMAS,
        relations=_REPORTING_RELATIONS,
        reporting_role=settings.reporting.username,
        owner_role=settings.maintenance.username,
    )
    run_psql_grants(
        settings=settings,
        sql=grant_sql,
        dry_run=args.dry_run,
    )


__all__ = [
    "build_reporting_grant_sql",
    "build_runtime_grant_sql",
    "configure_parser",
    "execute",
    "maintenance_database_url",
    "run_alembic",
    "run_psql_grants",
]
