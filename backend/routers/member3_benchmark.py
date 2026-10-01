"""Member 3: index benchmarks (planner toggle), materialized views, table stats."""
import asyncio
import statistics
import time

from fastapi import APIRouter, HTTPException

import db
from constants import PLAGIARISM_HASH, PLAGIARISM_PROBLEM_ID, RARE_TAG
from db import envelope, make_telemetry, xray_fetch

router = APIRouter(prefix="/api/benchmark", tags=["member3-benchmark"])

PLAG_SQL = ("SELECT submission_id, user_id, status::text AS status, submitted_at FROM submissions "
            "WHERE problem_id = $1 AND source_code_hash = $2 ORDER BY submitted_at")
GIN_SQL = "SELECT submission_id, user_id, language FROM submissions WHERE code_metadata @> $1::jsonb"
TT_SQL = ("SELECT event_id, new_points, new_penalty_sec, recorded_at FROM score_audit_ledger "
          "WHERE contest_id = $1 AND user_id = $2 AND recorded_at <= $3 ORDER BY recorded_at DESC LIMIT 1")

ANALYTICS_RAW_SQL = """
SELECT c.contest_id, c.title AS contest_title, p.problem_id, p.code AS problem_code, s.language,
       COUNT(*) AS total_submissions,
       COUNT(*) FILTER (WHERE s.status = 'ACCEPTED') AS accepted_count,
       ROUND(100.0 * COUNT(*) FILTER (WHERE s.status = 'ACCEPTED') / COUNT(*), 2) AS acceptance_rate_pct,
       ROUND(AVG(s.execution_time_ms) FILTER (WHERE s.status = 'ACCEPTED'), 1) AS avg_runtime_ms,
       COUNT(DISTINCT s.user_id) AS distinct_users
FROM submissions s
JOIN problems p ON p.problem_id = s.problem_id
JOIN contests c ON c.contest_id = p.contest_id
JOIN users u ON u.user_id = s.user_id
JOIN contest_registrations cr ON cr.contest_id = c.contest_id AND cr.user_id = u.user_id
WHERE c.contest_id = $1
GROUP BY c.contest_id, c.title, p.problem_id, p.code, s.language
ORDER BY p.code, s.language
"""
MV_SQL = "SELECT * FROM mv_contest_analytics WHERE contest_id = $1 ORDER BY problem_code, language"


async def explain_median(conn, sql: str, args: tuple, runs: int = 5):
    await conn.fetch(sql, *args)                                   # warm-up (fills cache)
    times, plan = [], None
    for _ in range(runs):
        plan = await conn.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, *args)
        times.append(plan[0]["Execution Time"])
    return statistics.median(times), times, plan


async def compare_scan(sql: str, args: tuple, label: str) -> dict:
    out = {}
    for use_index in (False, True):
        async with db.get_pool().acquire() as conn:
            async with conn.transaction():                          # SET LOCAL-style flags live only here
                mode = await conn.fetchval("SELECT fn_set_scan_mode($1)", use_index)
                median, times, plan = await explain_median(conn, sql, args)
                n_rows = len(await conn.fetch(sql, *args))
        tele = make_telemetry(sql, list(args), None, plan, mode)
        tele["execution_time_ms"] = round(median, 3)
        out["indexed" if use_index else "sequential"] = {"median_ms": round(median, 3), "runs_ms": [round(t, 3) for t in times],
                                                         "tele": tele, "rows": n_rows}
    seq, idx = out["sequential"], out["indexed"]
    speedup = round(seq["median_ms"] / idx["median_ms"], 1) if idx["median_ms"] > 0 else None
    data = {
        "benchmark": label, "rows_returned": idx["rows"],
        "sequential_ms": seq["median_ms"], "indexed_ms": idx["median_ms"], "speedup_x": speedup,
        "sequential_scan_type": seq["tele"]["scan_type"], "indexed_scan_type": idx["tele"]["scan_type"],
        "sequential_runs_ms": seq["runs_ms"], "indexed_runs_ms": idx["runs_ms"],
        "forced_seq_ok": "Seq Scan" in seq["tele"]["scan_type"],
        "indexed_used_index": "Index" in idx["tele"]["scan_type"],
    }
    return envelope(data, idx["tele"], [seq["tele"]])


@router.get("/plagiarism")
async def bench_plagiarism():
    return await compare_scan(PLAG_SQL, (PLAGIARISM_PROBLEM_ID, PLAGIARISM_HASH), "Plagiarism hash lookup (composite B-Tree)")


@router.get("/jsonb-gin")
async def bench_gin():
    return await compare_scan(GIN_SQL, ({"ast_tags": [RARE_TAG]},), f"JSONB AST tag search '{RARE_TAG}' (GIN)")


@router.get("/time-travel-lookup")
async def bench_tt():
    async with db.get_pool().acquire() as conn:
        row = await conn.fetchrow("""SELECT c.end_time - interval '1 hour' AS ts,
                                            (SELECT l.user_id FROM score_audit_ledger l WHERE l.contest_id = c.contest_id
                                             ORDER BY l.event_id LIMIT 1) AS uid
                                     FROM contests c WHERE c.contest_id = 1""")
    if row is None or row["uid"] is None:
        raise HTTPException(500, "No ledger data - run seed_100k.py")
    return await compare_scan(TT_SQL, (1, row["uid"], row["ts"]), "Single-user time-travel lookup (temporal B-Tree)")


@router.get("/mv-compare")
async def mv_compare(contest_id: int = 1):
    async with db.get_pool().acquire() as conn:
        raw_med, raw_times, raw_plan = await explain_median(conn, ANALYTICS_RAW_SQL, (contest_id,), runs=3)
        mv_med, mv_times, mv_plan = await explain_median(conn, MV_SQL, (contest_id,), runs=5)
        mv_rows = await conn.fetch(MV_SQL, contest_id)
        top = await conn.fetch("""SELECT handle, accepted_count, total_submissions FROM mv_top_performers
                                  WHERE contest_id = $1 ORDER BY accepted_count DESC, handle LIMIT 5""", contest_id)
    raw_t = make_telemetry(ANALYTICS_RAW_SQL, [contest_id], None, raw_plan, "Live 5-table join + aggregation")
    raw_t["execution_time_ms"] = round(raw_med, 3)
    mv_t = make_telemetry(MV_SQL, [contest_id], None, mv_plan, "Read from materialized view")
    mv_t["execution_time_ms"] = round(mv_med, 3)
    return envelope({
        "raw_join_ms": round(raw_med, 3), "materialized_view_ms": round(mv_med, 3),
        "speedup_x": round(raw_med / mv_med, 1) if mv_med > 0 else None,
        "rows": [dict(r) for r in mv_rows], "top_performers": [dict(t) for t in top],
    }, mv_t, [raw_t])


@router.post("/mv-refresh")
async def mv_refresh(contest_id: int = 1):
    pool = db.get_pool()

    async def do_refresh() -> float:
        async with pool.acquire() as c:
            t0 = time.perf_counter()
            await c.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_contest_analytics")
            await c.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_top_performers")
            return (time.perf_counter() - t0) * 1000

    task = asyncio.create_task(do_refresh())
    await asyncio.sleep(0.03)
    reads = []
    async with pool.acquire() as c:
        while not task.done() and len(reads) < 100:
            t0 = time.perf_counter()
            await c.fetch(MV_SQL, contest_id)
            reads.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.005)
    refresh_ms = await task
    tele = make_telemetry("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_contest_analytics;\nREFRESH MATERIALIZED VIEW CONCURRENTLY mv_top_performers;",
                          [], refresh_ms, None, "DDL-like command: EXPLAIN not applicable")
    return envelope({
        "refresh_ms": round(refresh_ms, 2), "reads_during_refresh": len(reads),
        "slowest_read_ms": round(max(reads), 2) if reads else None,
        "reads_blocked": bool(reads) and max(reads) > refresh_ms * 0.8,
    }, tele)


@router.get("/table-stats")
async def table_stats():
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, """
            SELECT relname AS table_name, n_live_tup AS approx_rows,
                   pg_size_pretty(pg_total_relation_size(relid)) AS total_size
            FROM pg_stat_user_tables ORDER BY n_live_tup DESC""", explain=False)
    return envelope(rows, tele)
