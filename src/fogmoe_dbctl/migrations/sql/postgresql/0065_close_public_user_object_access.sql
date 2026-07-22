-- migrate:up

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- @brief PUBLIC 是所有登录角色隐式继承的伪角色，必须独立于直接角色 ACL 收敛 /
-- PUBLIC is inherited by every login and must be converged independently of direct role ACLs.
DO $fogmoe_0065_revoke$
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
      'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC',
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC',
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC',
      target_schema.schema_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
      'REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC',
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
            AND extension.extname = 'vector'
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
            AND extension.extname = 'vector'
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
              AND extension.extname = 'vector'
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
              AND extension.extname = 'vector'
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
$fogmoe_0065_revoke$;

ALTER DEFAULT PRIVILEGES
  REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC;

ALTER DEFAULT PRIVILEGES
  REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES
  REVOKE ALL PRIVILEGES ON ROUTINES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES
  REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC;

-- @brief 应用在 PUBLIC schema 只获得 vector 名称解析，不能创建或读取任意用户对象 /
-- The application receives only vector name lookup in public, never CREATE or arbitrary user-object access.
GRANT USAGE ON SCHEMA public TO {{ application_role }};

DO $fogmoe_0065_application_types$
DECLARE
  target_schema RECORD;
  target_type RECORD;
BEGIN
  FOR target_schema IN
    SELECT unnest(ARRAY[
      'identity', 'conversation', 'context_window', 'retrieval', 'user_profile',
      'assistant', 'scheduling', 'economy', 'moderation', 'crypto', 'game',
      'media', 'admin', 'observability', 'bank', 'billing', 'town', 'chance',
      'personal_rpg'
    ]) AS schema_name
  LOOP
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
      'GRANT USAGE ON TYPES TO %I',
      target_schema.schema_name,
      {{ application_role_literal }}
    );
  END LOOP;

  FOR target_type IN
    SELECT namespace.nspname AS schema_name,
           data_type.typname AS type_name
    FROM pg_type AS data_type
    JOIN pg_namespace AS namespace ON namespace.oid = data_type.typnamespace
    WHERE namespace.nspname = ANY (ARRAY[
      'identity', 'conversation', 'context_window', 'retrieval', 'user_profile',
      'assistant', 'scheduling', 'economy', 'moderation', 'crypto', 'game',
      'media', 'admin', 'observability', 'bank', 'billing', 'town', 'chance',
      'personal_rpg'
    ])
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
      {{ application_role_literal }}
    );
  END LOOP;
END;
$fogmoe_0065_application_types$;

-- @brief vector 成员由 bootstrap superuser 拥有并保留扩展 ACL；PUBLIC schema 本身是访问门 /
-- Vector members keep superuser-owned extension ACLs; the public schema itself is the access gate.
DO $fogmoe_0065_guard$
BEGIN
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
            AND extension.extname = 'vector'
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
            AND extension.extname = 'vector'
        )
      )
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
              AND extension.extname = 'vector'
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
              AND extension.extname = 'vector'
          )
        )
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_largeobject_metadata AS metadata
    CROSS JOIN LATERAL aclexplode(metadata.lomacl) AS privilege
    WHERE privilege.grantee = 0
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
      'PUBLIC retains access to a non-system user object outside the trusted vector extension boundary'
      USING ERRCODE = '42501';
  END IF;
END;
$fogmoe_0065_guard$;

-- migrate:down

DO $fogmoe_0065_irreversible$
BEGIN
  RAISE EXCEPTION
    '0065 is irreversible because arbitrary historical PUBLIC ACL provenance cannot be reconstructed safely'
    USING ERRCODE = '0A000';
END;
$fogmoe_0065_irreversible$;
