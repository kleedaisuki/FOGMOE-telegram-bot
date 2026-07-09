-- migrate:up

ALTER TABLE assistant.ai_schedules
  ADD COLUMN recurrence_unit TEXT NOT NULL DEFAULT 'none'
    CHECK (recurrence_unit IN ('none','minute','hour','day')),
  ADD COLUMN recurrence_interval INT NOT NULL DEFAULT 1,
  ADD COLUMN last_run_at TIMESTAMP NULL DEFAULT NULL;

-- migrate:down

ALTER TABLE assistant.ai_schedules
  DROP COLUMN last_run_at,
  DROP COLUMN recurrence_interval,
  DROP COLUMN recurrence_unit;
