-- migrate:up

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '5min';

-- 有界维护窗口内一次取得 ALTER 所需最终锁，避免事务内锁升级 / Acquire the final
-- ALTER lock once in a bounded maintenance window to avoid an in-transaction lock upgrade.
LOCK TABLE user_profile.dreams IN ACCESS EXCLUSIVE MODE;

DO $dream_state_preflight$
DECLARE
  incompatible_count BIGINT;
BEGIN
  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status NOT IN (
    'pending', 'retry_wait', 'processing', 'completed', 'failed_final'
  );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have an unknown lifecycle state',
        incompatible_count
      ),
      HINT = 'Classify each job from authoritative worker evidence before retrying migration 0075; migration does not infer lifecycle state.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE user_id <= 0;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have a nonpositive owner identity',
        incompatible_count
      ),
      HINT = 'Reconcile each owner with the authoritative identity record before retrying migration 0075; owner identities are not reassigned automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE NOT (
    created_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
    AND created_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
    AND updated_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
    AND updated_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
    AND (
      next_attempt_at IS NULL
      OR (
        next_attempt_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND next_attempt_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
    AND (
      lease_expires_at IS NULL
      OR (
        lease_expires_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND lease_expires_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
    AND (
      completed_at IS NULL
      OR (
        completed_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND completed_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
  );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) contain timestamps outside the Python datetime range',
        incompatible_count
      ),
      HINT = 'Recover finite UTC instants in years 1 through 9999 before retrying migration 0075; infinity, BC, and year 10000+ values are not guessed or clamped.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE NOT CASE
      WHEN jsonb_typeof(metadata) = 'object' THEN
        CASE
          WHEN
            jsonb_typeof(metadata -> 'display_name') = 'string'
            AND char_length(btrim(metadata ->> 'display_name', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 256
            AND (
              NOT (metadata ? 'username')
              OR metadata -> 'username' = 'null'::JSONB
              OR (
                jsonb_typeof(metadata -> 'username') = 'string'
                AND char_length(btrim(metadata ->> 'username', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 64
              )
            )
            AND (
              NOT (metadata ? 'personal_info')
              OR (
                jsonb_typeof(metadata -> 'personal_info') = 'string'
                AND char_length(metadata ->> 'personal_info') <= 500
              )
            )
            AND (
              NOT (metadata ? 'provider')
              OR (
                jsonb_typeof(metadata -> 'provider') = 'string'
                AND (metadata ->> 'provider') ~ '^[a-z][a-z0-9_.-]{0,31}$'
                AND char_length(btrim(metadata ->> 'provider', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 32
              )
            )
          THEN TRUE
          ELSE FALSE
        END
      ELSE FALSE
    END;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have metadata that cannot hydrate',
        incompatible_count
      ),
      HINT = 'Recover canonical display_name, username, personal_info, and provider values before retrying migration 0075; metadata is not rewritten automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE version <> attempt_count;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have version differing from attempt_count',
        incompatible_count
      ),
      HINT = 'Reconcile version and attempt_count with audited claim history before retrying migration 0075; counters are not rewritten automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'pending'
    AND (version <> 0 OR attempt_count <> 0);
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s pending row(s) have prior-claim counters',
        incompatible_count
      ),
      HINT = 'Establish whether each job was claimed before retrying migration 0075; pending counters are not reset automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status <> 'pending' AND attempt_count < 1;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s non-pending row(s) lack a prior claim',
        incompatible_count
      ),
      HINT = 'Reconcile the missing claim history before retrying migration 0075; migration does not fabricate attempts.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE created_at > updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have updated_at before created_at',
        incompatible_count
      ),
      HINT = 'Repair timestamps from authoritative records before retrying migration 0075; timestamps are not reordered automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE (
      status IN ('pending', 'retry_wait')
      AND next_attempt_at IS NULL
    ) OR (
      status IN ('processing', 'completed', 'failed_final')
      AND next_attempt_at IS NOT NULL
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have a schedule outside a waiting state',
        incompatible_count
      ),
      HINT = 'Recover the authoritative scheduling decision before retrying migration 0075; schedules are neither created nor discarded automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE (
      status = 'processing'
      AND (claim_token IS NULL OR lease_expires_at IS NULL)
    ) OR (
      status <> 'processing'
      AND (claim_token IS NOT NULL OR lease_expires_at IS NOT NULL)
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have lease fields outside complete processing ownership',
        incompatible_count
      ),
      HINT = 'Resolve ownership through normal settlement or recovery before retrying migration 0075; lease capabilities are not invented or cleared.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE (
      status IN ('completed', 'failed_final')
      AND completed_at IS NULL
    ) OR (
      status IN ('pending', 'retry_wait', 'processing')
      AND completed_at IS NOT NULL
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have a completion time outside a terminal state',
        incompatible_count
      ),
      HINT = 'Reconcile terminal outcomes from authoritative records before retrying migration 0075; completion times are not synthesized or removed.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE (
      status = 'completed'
      AND (
        result_patch IS NULL
        OR route_key IS NULL
        OR char_length(route_key) NOT BETWEEN 1 AND 300
        OR char_length(btrim(route_key, E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) = 0
      )
    ) OR (
      status <> 'completed'
      AND (result_patch IS NOT NULL OR route_key IS NOT NULL)
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have result or route fields outside completed state',
        incompatible_count
      ),
      HINT = 'Reconcile the model result and route from authoritative generation output before retrying migration 0075; results are not fabricated or discarded.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'completed'
    AND NOT CASE
      WHEN jsonb_typeof(result_patch) = 'object'
        AND jsonb_typeof(result_patch -> 'operations') = 'array'
        AND jsonb_typeof(result_patch -> 'prompt_version') = 'number'
      THEN
        (result_patch ->> 'prompt_version') ~ '^[1-9][0-9]*$'
        AND jsonb_array_length(result_patch -> 'operations') <= 64
      ELSE FALSE
    END;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s completed row(s) have an invalid result envelope',
        incompatible_count
      ),
      HINT = 'Recover prompt_version and operations from authoritative generation output before retrying migration 0075; result envelopes are not repaired automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'completed'
    AND (
      jsonb_path_exists(
      result_patch,
      '$.operations[*] ? (
        @.type() != "object"
        || !(exists(@.op) && @.op.type() == "string" && (@.op == "delete" || @.op == "upsert"))
        || !(exists(@.key) && @.key.type() == "string" && @.key like_regex "^[a-z][a-z0-9_.-]{0,79}$")
        || !(exists(@.evidence_event_ids) && @.evidence_event_ids.type() == "array" && @.evidence_event_ids.size() >= 1 && @.evidence_event_ids.size() <= 16)
        || exists(@.evidence_event_ids[*] ? (@.type() != "number" || @ <= 0 || @ > 9223372036854775807 || @ != @.floor()))
        || (@.op == "delete" && exists(@.keyvalue() ? (@.key != "op" && @.key != "key" && @.key != "evidence_event_ids")))
        || (@.op == "upsert" && (
             !(exists(@.kind) && @.kind.type() == "string" && (@.kind == "fact" || @.kind == "preference" || @.kind == "goal" || @.kind == "interaction_style"))
             || !(exists(@.confidence) && @.confidence.type() == "string" && (@.confidence == "explicit" || @.confidence == "inferred"))
             || !(exists(@.statement) && @.statement.type() == "string" && @.statement like_regex "^.{1,250}(.{1,250})?$" flag "s" && @.statement like_regex ".*[^\u0009\u000A\u000B\u000C\u000D\u001C\u001D\u001E\u001F\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000].*" flag "s")
             || exists(@.keyvalue() ? (@.key != "op" && @.key != "key" && @.key != "kind" && @.key != "statement" && @.key != "confidence" && @.key != "evidence_event_ids"))
           ))
      )'
      )
      OR NOT (
        jsonb_path_query_array(
          result_patch,
          '$.operations[*].evidence_event_ids[*]'
        )::TEXT ~ $integer_ids$^\[([1-9][0-9]*([[:space:]]*,[[:space:]]*[1-9][0-9]*)*)?\]$$integer_ids$
      )
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s completed row(s) contain a malformed result operation',
        incompatible_count
      ),
      HINT = 'Recover canonical operations from authoritative generation output before retrying migration 0075; malformed operations are not discarded or normalized.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE (
      status IN ('retry_wait', 'failed_final')
      AND (
        last_error IS NULL
        OR char_length(last_error) NOT BETWEEN 1 AND 1000
        OR char_length(btrim(last_error, E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) = 0
      )
    ) OR (
      status IN ('pending', 'processing', 'completed')
      AND last_error IS NOT NULL
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s row(s) have an error outside a failure state',
        incompatible_count
      ),
      HINT = 'Recover or classify the audited failure before retrying migration 0075; error summaries are not invented or discarded.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'pending'
    AND (
      next_attempt_at IS DISTINCT FROM created_at
      OR updated_at IS DISTINCT FROM created_at
    );
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s pending row(s) have noncanonical initial timestamps',
        incompatible_count
      ),
      HINT = 'Verify the enqueue instant before retrying migration 0075; untouched pending timestamps are not rewritten.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'processing' AND lease_expires_at <= updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s processing row(s) have a nonfuture lease',
        incompatible_count
      ),
      HINT = 'Resolve the expired or malformed claim through normal recovery before retrying migration 0075; migration does not steal leases.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status = 'retry_wait' AND next_attempt_at < updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s retry row(s) are scheduled before updated_at',
        incompatible_count
      ),
      HINT = 'Repair retry timing from authoritative failure records before retrying migration 0075; schedules are not shifted automatically.';
  END IF;

  SELECT count(*) INTO incompatible_count
  FROM user_profile.dreams
  WHERE status IN ('completed', 'failed_final')
    AND completed_at IS DISTINCT FROM updated_at;
  IF incompatible_count > 0 THEN
    RAISE EXCEPTION USING
      MESSAGE = format(
        'user-profile Dream preflight: %s terminal row(s) have divergent completion timestamps',
        incompatible_count
      ),
      HINT = 'Reconcile terminal commit times from authoritative records before retrying migration 0075; neither timestamp is guessed.';
  END IF;
END
$dream_state_preflight$;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_owner_ck CHECK (
    user_id > 0
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_owner_ck;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_timestamp_range_ck CHECK (
    created_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
    AND created_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
    AND updated_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
    AND updated_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
    AND (
      next_attempt_at IS NULL
      OR (
        next_attempt_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND next_attempt_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
    AND (
      lease_expires_at IS NULL
      OR (
        lease_expires_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND lease_expires_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
    AND (
      completed_at IS NULL
      OR (
        completed_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND completed_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
      )
    )
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_timestamp_range_ck;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_counter_ck CHECK (
    version = attempt_count
    AND (
      (status = 'pending' AND version = 0 AND attempt_count = 0)
      OR
      (status <> 'pending' AND version >= 1 AND attempt_count >= 1)
    )
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_counter_ck;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_metadata_ck CHECK (
    CASE
      WHEN jsonb_typeof(metadata) = 'object' THEN
        CASE
          WHEN
            jsonb_typeof(metadata -> 'display_name') = 'string'
            AND char_length(btrim(metadata ->> 'display_name', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 256
            AND (
              NOT (metadata ? 'username')
              OR metadata -> 'username' = 'null'::JSONB
              OR (
                jsonb_typeof(metadata -> 'username') = 'string'
                AND char_length(btrim(metadata ->> 'username', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 64
              )
            )
            AND (
              NOT (metadata ? 'personal_info')
              OR (
                jsonb_typeof(metadata -> 'personal_info') = 'string'
                AND char_length(metadata ->> 'personal_info') <= 500
              )
            )
            AND (
              NOT (metadata ? 'provider')
              OR (
                jsonb_typeof(metadata -> 'provider') = 'string'
                AND (metadata ->> 'provider') ~ '^[a-z][a-z0-9_.-]{0,31}$'
                AND char_length(btrim(metadata ->> 'provider', E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) BETWEEN 1 AND 32
              )
            )
          THEN TRUE
          ELSE FALSE
        END
      ELSE FALSE
    END
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_metadata_ck;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_state_ck CHECK (
    (
      status = 'pending'
      AND next_attempt_at IS NOT NULL
      AND next_attempt_at = created_at
      AND updated_at = created_at
      AND claim_token IS NULL
      AND lease_expires_at IS NULL
      AND result_patch IS NULL
      AND route_key IS NULL
      AND last_error IS NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'processing'
      AND next_attempt_at IS NULL
      AND claim_token IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at > updated_at
      AND result_patch IS NULL
      AND route_key IS NULL
      AND last_error IS NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'retry_wait'
      AND next_attempt_at IS NOT NULL
      AND next_attempt_at >= updated_at
      AND claim_token IS NULL
      AND lease_expires_at IS NULL
      AND result_patch IS NULL
      AND route_key IS NULL
      AND last_error IS NOT NULL
      AND char_length(last_error) BETWEEN 1 AND 1000
      AND char_length(btrim(last_error, E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) > 0
      AND completed_at IS NULL
    )
    OR (
      status = 'completed'
      AND next_attempt_at IS NULL
      AND claim_token IS NULL
      AND lease_expires_at IS NULL
      AND result_patch IS NOT NULL
      AND route_key IS NOT NULL
      AND char_length(route_key) BETWEEN 1 AND 300
      AND char_length(btrim(route_key, E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) > 0
      AND last_error IS NULL
      AND completed_at IS NOT NULL
      AND completed_at = updated_at
    )
    OR (
      status = 'failed_final'
      AND next_attempt_at IS NULL
      AND claim_token IS NULL
      AND lease_expires_at IS NULL
      AND result_patch IS NULL
      AND route_key IS NULL
      AND last_error IS NOT NULL
      AND char_length(last_error) BETWEEN 1 AND 1000
      AND char_length(btrim(last_error, E' \t\n\r\f\v' || U&'\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')) > 0
      AND completed_at IS NOT NULL
      AND completed_at = updated_at
    )
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_state_ck;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_result_payload_ck CHECK (
    status <> 'completed'
    OR CASE
      WHEN jsonb_typeof(result_patch) = 'object'
        AND jsonb_typeof(result_patch -> 'operations') = 'array'
        AND jsonb_typeof(result_patch -> 'prompt_version') = 'number'
      THEN
        (result_patch ->> 'prompt_version') ~ '^[1-9][0-9]*$'
        AND jsonb_array_length(result_patch -> 'operations') <= 64
        AND NOT jsonb_path_exists(
          result_patch,
          '$.operations[*] ? (
            @.type() != "object"
            || !(exists(@.op) && @.op.type() == "string" && (@.op == "delete" || @.op == "upsert"))
            || !(exists(@.key) && @.key.type() == "string" && @.key like_regex "^[a-z][a-z0-9_.-]{0,79}$")
            || !(exists(@.evidence_event_ids) && @.evidence_event_ids.type() == "array" && @.evidence_event_ids.size() >= 1 && @.evidence_event_ids.size() <= 16)
            || exists(@.evidence_event_ids[*] ? (@.type() != "number" || @ <= 0 || @ > 9223372036854775807 || @ != @.floor()))
            || (@.op == "delete" && exists(@.keyvalue() ? (@.key != "op" && @.key != "key" && @.key != "evidence_event_ids")))
            || (@.op == "upsert" && (
                 !(exists(@.kind) && @.kind.type() == "string" && (@.kind == "fact" || @.kind == "preference" || @.kind == "goal" || @.kind == "interaction_style"))
                 || !(exists(@.confidence) && @.confidence.type() == "string" && (@.confidence == "explicit" || @.confidence == "inferred"))
                 || !(exists(@.statement) && @.statement.type() == "string" && @.statement like_regex "^.{1,250}(.{1,250})?$" flag "s" && @.statement like_regex ".*[^\u0009\u000A\u000B\u000C\u000D\u001C\u001D\u001E\u001F\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000].*" flag "s")
                 || exists(@.keyvalue() ? (@.key != "op" && @.key != "key" && @.key != "kind" && @.key != "statement" && @.key != "confidence" && @.key != "evidence_event_ids"))
               ))
          )'
        )
        AND jsonb_path_query_array(
          result_patch,
          '$.operations[*].evidence_event_ids[*]'
        )::TEXT ~ $integer_ids$^\[([1-9][0-9]*([[:space:]]*,[[:space:]]*[1-9][0-9]*)*)?\]$$integer_ids$
      ELSE FALSE
    END
  ) NOT VALID;

ALTER TABLE user_profile.dreams
  VALIDATE CONSTRAINT user_profile_dreams_result_payload_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_ready_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_lease_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_terminal_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_result_ck;

-- migrate:down

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '5min';

-- 降级同样预先取得最终 DDL 锁 / Downgrade also acquires its final DDL lock upfront.
LOCK TABLE user_profile.dreams IN ACCESS EXCLUSIVE MODE;

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_ready_ck CHECK (
    (status IN ('pending','retry_wait')) = (next_attempt_at IS NOT NULL)
  );

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_lease_ck CHECK (
    (status = 'processing') = (
      claim_token IS NOT NULL AND lease_expires_at IS NOT NULL
    )
  );

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_terminal_ck CHECK (
    (status IN ('completed','failed_final')) = (completed_at IS NOT NULL)
  );

ALTER TABLE user_profile.dreams
  ADD CONSTRAINT user_profile_dreams_result_ck CHECK (
    status <> 'completed' OR (
      result_patch IS NOT NULL AND route_key IS NOT NULL
    )
  );

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_state_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_counter_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_result_payload_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_metadata_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_timestamp_range_ck;

ALTER TABLE user_profile.dreams
  DROP CONSTRAINT user_profile_dreams_owner_ck;
