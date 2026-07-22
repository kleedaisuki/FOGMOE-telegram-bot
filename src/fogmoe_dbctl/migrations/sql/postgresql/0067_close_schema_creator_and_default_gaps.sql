-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- @brief 将迁移 owner 与非系统 schema 的创建权收敛为可证明的单一边界 /
-- Converge the migration owner and non-system schema creators to one provable boundary.
DO $fogmoe_0067$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles AS migration_role
    WHERE migration_role.rolname = current_user
      AND migration_role.rolcanlogin
      AND NOT migration_role.rolsuper
  ) THEN
    RAISE EXCEPTION
      '0067 must run as the non-superuser LOGIN maintenance owner'
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
      AND candidate_role.rolname <> current_user
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

  -- An absent pg_default_acl row means PostgreSQL's built-in PUBLIC default,
  -- so both global overrides must exist rather than merely have no visible grant.
  IF EXISTS (
    SELECT 1
    FROM (VALUES ('f'::"char"), ('T'::"char")) AS required_acl(object_type)
    WHERE NOT EXISTS (
      SELECT 1
      FROM pg_default_acl AS default_acl
      JOIN pg_roles AS owner_role
        ON owner_role.oid = default_acl.defaclrole
      WHERE owner_role.rolname = current_user
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
END;
$fogmoe_0067$;

-- migrate:down

-- Validation-only revision: no database state is changed by upgrade.
SELECT 1;
