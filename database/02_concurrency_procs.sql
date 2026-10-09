-- 02_concurrency_procs.sql: Worker Queue & Concurrency Management
CREATE INDEX IF NOT EXISTS idx_submission_queue_claim
    ON submission_queue (priority ASC, enqueued_at ASC)
    WHERE locked_by_worker_id IS NULL;
