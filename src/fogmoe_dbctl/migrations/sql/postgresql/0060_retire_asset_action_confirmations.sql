-- migrate:up

-- 0059 must remain in source control because deployed databases can already be
-- stamped at that revision.  This is the forward-only rollback of its schema:
-- do not remove the table when it contains any confirmation record, because a
-- record may describe an unexecuted account-changing action and requires a
-- human audit rather than destructive automation.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

LOCK TABLE assistant.asset_action_confirmations IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM assistant.asset_action_confirmations
  ) THEN
    RAISE EXCEPTION
      'cannot retire asset action confirmations: nonempty table requires manual audit and archival'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

DROP TABLE assistant.asset_action_confirmations;

-- migrate:down

DO $$
BEGIN
  RAISE EXCEPTION
    '0060_retire_asset_action_confirmations is irreversible: the reverted feature has no runtime owner and dropped records cannot be reconstructed safely'
    USING ERRCODE = '55000';
END;
$$;
