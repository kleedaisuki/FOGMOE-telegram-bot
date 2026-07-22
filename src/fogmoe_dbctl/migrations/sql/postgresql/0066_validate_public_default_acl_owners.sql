-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- @brief 任意 owner 的 PUBLIC default ACL 都会在未来重新打开访问面，必须失败关闭 /
-- A PUBLIC default ACL owned by any role can reopen future access and must fail closed.
DO $fogmoe_0066$
BEGIN
  IF EXISTS (
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
      'a role retains PUBLIC default ACLs for a non-system user-object scope'
      USING ERRCODE = '42501';
  END IF;
END;
$fogmoe_0066$;

-- migrate:down

-- Validation-only revision: no database state is changed by upgrade.
SELECT 1;
