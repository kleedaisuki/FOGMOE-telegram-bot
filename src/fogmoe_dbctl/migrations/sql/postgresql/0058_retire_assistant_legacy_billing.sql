-- migrate:up

-- Migration 0054 reconciled every native Assistant reservation as an audited
-- Bank refund and froze the table.  A non-terminal row here means that a
-- deployment was only partially migrated or its historical data was tampered
-- with; dropping it would erase an unresolved monetary fact, so fail closed.
-- The locks also make a mixed-version rollout wait rather than allowing a
-- legacy writer to race the retirement DDL.
LOCK TABLE
  assistant.billing_reservations,
  economy.kindness_gifts
IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM assistant.billing_reservations AS reservation
    WHERE reservation.status NOT IN ('settled', 'released')
  ) THEN
    RAISE EXCEPTION
      'cannot retire Assistant billing: an unresolved historical reservation requires manual audit'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

-- The trigger belongs solely to this retired table.  Drop it explicitly before
-- its function so no unrelated dependency is silently cascaded.
DROP TRIGGER IF EXISTS assistant_billing_reservations_retired_tr
  ON assistant.billing_reservations;
DROP FUNCTION IF EXISTS bank.forbid_legacy_assistant_billing_mutation();

-- ``assistant.billing_reservations`` contains no surviving runtime state: the
-- Bank ledger and its immutable migration entries remain the monetary audit
-- authority.  ``kindness_gifts`` is an unreferenced no-op table and has never
-- been a balance authority.
DROP TABLE assistant.billing_reservations;
DROP TABLE economy.kindness_gifts;

-- migrate:down

DO $$
BEGIN
  RAISE EXCEPTION
    '0058_retire_assistant_legacy_billing is irreversible: deleted legacy rows have no lossless reconstruction and Bank ledger history remains authoritative'
    USING ERRCODE = '55000';
END;
$$;
