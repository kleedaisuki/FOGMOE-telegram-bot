-- migrate:up

-- PostgreSQL checks a foreign key by taking KEY SHARE on the referenced row.
-- That lock conflicts with the reward-debit gate's FOR UPDATE, so even an
-- append-only Assistant credit would queue behind a reward claim.  pool_id is
-- a routing key, not aggregate ownership; the posting ledger deliberately
-- remains independent from the singleton withdrawal gate.
ALTER TABLE economy.stake_pool_postings
  DROP CONSTRAINT IF EXISTS stake_pool_postings_pool_id_fkey;

-- migrate:down

ALTER TABLE economy.stake_pool_postings
  ADD CONSTRAINT stake_pool_postings_pool_id_fkey
  FOREIGN KEY (pool_id)
  REFERENCES economy.stake_reward_pool(id)
  ON DELETE RESTRICT
  NOT VALID;

ALTER TABLE economy.stake_pool_postings
  VALIDATE CONSTRAINT stake_pool_postings_pool_id_fkey;
