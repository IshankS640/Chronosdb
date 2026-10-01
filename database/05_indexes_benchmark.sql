-- =====================================================================
-- ChronosDB | 05_indexes_benchmark.sql | indexes + planner toggle
-- =====================================================================

-- Plagiarism lookup
CREATE INDEX idx_submissions_plagiarism ON submissions (problem_id, source_code_hash, status);

-- Time-travel lookups
CREATE INDEX idx_ledger_time_travel ON score_audit_ledger (contest_id, user_id, recorded_at DESC);

-- JSONB containment (@>) search
CREATE INDEX idx_submissions_ast_gin ON submissions USING GIN (code_metadata);

-- Transaction-local switch: FALSE => planner is forbidden from using any index
-- (forces Seq Scan); TRUE => normal planning. Must be called inside BEGIN...COMMIT.
CREATE OR REPLACE FUNCTION fn_set_scan_mode(p_use_index BOOLEAN) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE v TEXT := CASE WHEN p_use_index THEN 'on' ELSE 'off' END;
BEGIN
    PERFORM set_config('enable_indexscan',     v, true);
    PERFORM set_config('enable_indexonlyscan', v, true);
    PERFORM set_config('enable_bitmapscan',    v, true);
    RETURN 'enable_indexscan/indexonlyscan/bitmapscan = ' || v;
END $$;
