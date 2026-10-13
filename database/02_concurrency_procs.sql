-- =====================================================================
-- ChronosDB | 02_concurrency_procs.sql | worker queue + lock telemetry
-- =====================================================================

-- Partial index: only UNLOCKED jobs, already in claim order. Makes SKIP LOCKED fast.
CREATE INDEX idx_queue_pending
    ON submission_queue (priority DESC, queue_id ASC)
    WHERE locked_by_worker_id IS NULL;

-- Atomic job claim (single statement => atomic). Same SQL the API runs in SKIP LOCKED mode.
CREATE OR REPLACE FUNCTION fn_claim_next_job(p_worker_id INT)
RETURNS TABLE (queue_id BIGINT, submission_id BIGINT)
LANGUAGE sql AS $$
    WITH next_job AS (
        SELECT q.queue_id, q.submission_id
        FROM submission_queue q
        WHERE q.locked_by_worker_id IS NULL
        ORDER BY q.priority DESC, q.queue_id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE submission_queue sq
       SET locked_by_worker_id = p_worker_id,
           locked_at           = clock_timestamp(),
           attempt_count       = sq.attempt_count + 1
      FROM next_job
     WHERE sq.queue_id = next_job.queue_id
    RETURNING sq.queue_id, sq.submission_id;
$$;

-- Live lock telemetry (pg_locks JOIN pg_stat_activity).
-- NOTE: PostgreSQL stores row locks in the tuple header, not in pg_locks. What you
-- SEE here is the table-level lock (RowShareLock / RowExclusiveLock) and the
-- transaction-id lock of every session that currently holds row locks.
CREATE OR REPLACE VIEW v_lock_telemetry AS
SELECT l.pid,
       a.state,
       a.wait_event_type,
       a.wait_event,
       l.locktype,
       l.mode,
       l.granted,
       COALESCE(c.relname, '')            AS relation,
       l.transactionid::text              AS transaction_id,
       a.xact_start,
       left(a.query, 90)                  AS query
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
LEFT JOIN pg_class c    ON c.oid = l.relation
WHERE a.datname = current_database()
  AND a.pid <> pg_backend_pid()
  AND l.locktype IN ('relation', 'transactionid', 'tuple')
  AND (c.relname IS NULL
       OR c.relname IN ('submission_queue', 'submissions', 'leaderboard_scores', 'judge_workers'));
