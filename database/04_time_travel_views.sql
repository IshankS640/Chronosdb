-- =====================================================================
-- ChronosDB | 04_time_travel_views.sql | time travel + materialized views
-- =====================================================================

-- Mechanism C: point-in-time reconstruction of the leaderboard
CREATE OR REPLACE FUNCTION fn_reconstruct_leaderboard_at(p_contest_id INT, p_target_time TIMESTAMPTZ)
RETURNS TABLE (rank_pos BIGINT, user_id INT, handle VARCHAR,
               points_at_time INT, penalty_at_time INT, last_event_time TIMESTAMPTZ)
LANGUAGE sql STABLE AS $$
    WITH latest AS (
        SELECT DISTINCT ON (sal.user_id)
               sal.user_id         AS uid,
               sal.new_points      AS pts,
               sal.new_penalty_sec AS pen,
               sal.recorded_at     AS ts
        FROM score_audit_ledger sal
        WHERE sal.contest_id = p_contest_id
          AND sal.recorded_at <= p_target_time
        ORDER BY sal.user_id, sal.recorded_at DESC, sal.event_id DESC
    )
    SELECT RANK() OVER (ORDER BY l.pts DESC, l.pen ASC), l.uid, u.handle, l.pts, l.pen, l.ts
    FROM latest l
    JOIN users u ON u.user_id = l.uid
    ORDER BY 1, 3;
$$;

-- 5-table join (submissions, problems, contests, users, contest_registrations)
CREATE MATERIALIZED VIEW mv_contest_analytics AS
SELECT c.contest_id,
       c.title                                                         AS contest_title,
       p.problem_id,
       p.code                                                          AS problem_code,
       s.language,
       COUNT(*)                                                        AS total_submissions,
       COUNT(*) FILTER (WHERE s.status = 'ACCEPTED')                   AS accepted_count,
       ROUND(100.0 * COUNT(*) FILTER (WHERE s.status = 'ACCEPTED') / COUNT(*), 2) AS acceptance_rate_pct,
       ROUND(AVG(s.execution_time_ms) FILTER (WHERE s.status = 'ACCEPTED'), 1)    AS avg_runtime_ms,
       COUNT(DISTINCT s.user_id)                                       AS distinct_users
FROM submissions s
JOIN problems p              ON p.problem_id = s.problem_id
JOIN contests c              ON c.contest_id = p.contest_id
JOIN users u                 ON u.user_id    = s.user_id
JOIN contest_registrations cr ON cr.contest_id = c.contest_id AND cr.user_id = u.user_id
GROUP BY c.contest_id, c.title, p.problem_id, p.code, s.language
WITH DATA;

-- REQUIRED for REFRESH ... CONCURRENTLY
CREATE UNIQUE INDEX uq_mv_contest_analytics ON mv_contest_analytics (contest_id, problem_id, language);

CREATE MATERIALIZED VIEW mv_top_performers AS
SELECT c.contest_id,
       u.user_id,
       u.handle,
       COUNT(*)                                      AS total_submissions,
       COUNT(*) FILTER (WHERE s.status = 'ACCEPTED') AS accepted_count
FROM submissions s
JOIN problems p              ON p.problem_id = s.problem_id
JOIN contests c              ON c.contest_id = p.contest_id
JOIN users u                 ON u.user_id    = s.user_id
JOIN contest_registrations cr ON cr.contest_id = c.contest_id AND cr.user_id = u.user_id
GROUP BY c.contest_id, u.user_id, u.handle
WITH DATA;

CREATE UNIQUE INDEX uq_mv_top_performers ON mv_top_performers (contest_id, user_id);
