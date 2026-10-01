"""Member 1: worker swarm, SKIP LOCKED vs naive, ACID chaos crash, lock telemetry."""
import asyncio
import random
import time
from typing import Literal

import asyncpg
from fastapi import APIRouter, HTTPException

import db
from constants import DEMO_CONTEST_ID  # noqa: F401  (kept for symmetry)
from db import envelope, make_telemetry, xray_fetch

router = APIRouter(prefix="/api/concurrency", tags=["member1-concurrency"])

DEMO_PROBLEM_ID = 1
_background: set = set()          # keeps references to fire-and-forget tasks

ENQUEUE_SQL = """
WITH ins AS (
    INSERT INTO submissions (problem_id, user_id, language, source_code_hash)
    SELECT $1::int, $2::int, 'PYTHON3', encode(digest(gen_random_uuid()::text, 'sha256'), 'hex')
    FROM generate_series(1, $3::int)
    RETURNING submission_id
)
INSERT INTO submission_queue (submission_id)
SELECT submission_id FROM ins
RETURNING queue_id, submission_id
"""

SKIP_LOCKED_SQL = """
WITH next_job AS (
    SELECT queue_id, submission_id
    FROM submission_queue
    WHERE locked_by_worker_id IS NULL
    ORDER BY priority DESC, queue_id ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE submission_queue sq
   SET locked_by_worker_id = $1,
       locked_at = clock_timestamp(),
       attempt_count = sq.attempt_count + 1
  FROM next_job
 WHERE sq.queue_id = next_job.queue_id
RETURNING sq.queue_id, sq.submission_id
"""

NAIVE_SELECT_SQL = """
SELECT queue_id, submission_id FROM submission_queue
WHERE locked_by_worker_id IS NULL
ORDER BY priority DESC, queue_id ASC
LIMIT 1
"""
NAIVE_UPDATE_SQL = """
UPDATE submission_queue SET locked_by_worker_id = $1, locked_at = clock_timestamp()
WHERE queue_id = $2
"""


async def enqueue(conn: asyncpg.Connection, n: int) -> list:
    user_id = await conn.fetchval("SELECT user_id FROM users WHERE role = 'CONTESTANT' ORDER BY user_id LIMIT 1")
    if user_id is None:
        raise HTTPException(500, "No users found - did you run seed_100k.py?")
    rows = await conn.fetch(ENQUEUE_SQL, DEMO_PROBLEM_ID, user_id, n)
    return [r["submission_id"] for r in rows]


async def naive_claim(conn, wid):
    row = await conn.fetchrow(NAIVE_SELECT_SQL)
    if row is None:
        return None
    await asyncio.sleep(random.uniform(0.001, 0.006))      # the race window between SELECT and UPDATE
    await conn.execute(NAIVE_UPDATE_SQL, wid, row["queue_id"])
    return row


async def skip_claim(conn, wid):
    async with conn.transaction():
        return await conn.fetchrow(SKIP_LOCKED_SQL, wid)


async def worker_loop(pool, wid: int, mode: str, claims: list) -> int:
    mine = 0
    claim = naive_claim if mode == "naive" else skip_claim
    async with pool.acquire() as conn:
        await conn.execute("UPDATE judge_workers SET status='BUSY', last_heartbeat=clock_timestamp() WHERE worker_id=$1", wid)
        while True:
            row = await claim(conn, wid)
            if row is None:
                break
            claims.append((wid, row["queue_id"], row["submission_id"]))
            mine += 1
            await asyncio.sleep(0.002)                      # pretend to judge the code
            await conn.execute("DELETE FROM submission_queue WHERE queue_id = $1", row["queue_id"])
            await conn.execute("UPDATE submissions SET status='ACCEPTED', execution_time_ms=$2 WHERE submission_id=$1",
                               row["submission_id"], random.randint(20, 900))
        await conn.execute("""UPDATE judge_workers SET status='IDLE', processed_count = processed_count + $2,
                              last_heartbeat = clock_timestamp() WHERE worker_id = $1""", wid, mine)
    return mine


async def run_swarm(mode: str, n_jobs: int) -> dict:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await enqueue(conn, n_jobs)
        worker_ids = [r["worker_id"] for r in await conn.fetch("SELECT worker_id FROM judge_workers ORDER BY worker_id LIMIT 8")]
    if not worker_ids:
        raise HTTPException(500, "No judge_workers found - did you run seed_100k.py?")
    claims: list = []
    t0 = time.perf_counter()
    per_worker = await asyncio.gather(*[worker_loop(pool, w, mode, claims) for w in worker_ids])
    duration = (time.perf_counter() - t0) * 1000
    total = len(claims)
    distinct = len({c[2] for c in claims})
    async with pool.acquire() as conn:
        left = await conn.fetchval("SELECT count(*) FROM submission_queue")
    return {
        "mode": mode, "jobs_enqueued": n_jobs, "workers": len(worker_ids),
        "total_claims": total, "distinct_jobs_claimed": distinct,
        "collisions": total - distinct, "duration_ms": round(duration, 2),
        "claims_per_worker": dict(zip([str(w) for w in worker_ids], per_worker)),
        "queue_left": left,
    }


@router.get("/workers")
async def workers():
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, "SELECT worker_id, worker_name, status::text AS status, last_heartbeat, processed_count FROM judge_workers ORDER BY worker_id")
        depth, tele2 = await xray_fetch(conn, """SELECT count(*) FILTER (WHERE locked_by_worker_id IS NULL) AS pending,
                                                  count(*) FILTER (WHERE locked_by_worker_id IS NOT NULL) AS locked
                                           FROM submission_queue""")
    return envelope({"workers": rows, "queue": depth[0]}, tele, [tele2])


@router.post("/fire")
async def fire(mode: Literal["naive", "skip_locked"] = "skip_locked", n: int = 100):
    if not 1 <= n <= 500:
        raise HTTPException(400, "n must be between 1 and 500")
    result = await run_swarm(mode, n)
    sql = NAIVE_SELECT_SQL + ";\n" + NAIVE_UPDATE_SQL if mode == "naive" else SKIP_LOCKED_SQL
    tele = make_telemetry(sql, [], result["duration_ms"], None, "Write path: EXPLAIN ANALYZE skipped so jobs are not executed twice. Time = whole swarm run.")
    return envelope(result, tele)


@router.post("/fire-compare")
async def fire_compare(n: int = 100):
    if not 1 <= n <= 500:
        raise HTTPException(400, "n must be between 1 and 500")
    naive = await run_swarm("naive", n)
    safe = await run_swarm("skip_locked", n)
    tele = make_telemetry(SKIP_LOCKED_SQL, [], safe["duration_ms"], None, "Telemetry shows the SKIP LOCKED claim statement; naive run is in xray_extra.")
    tele_naive = make_telemetry(NAIVE_SELECT_SQL + ";\n" + NAIVE_UPDATE_SQL, [], naive["duration_ms"], None, "Naive SELECT-then-UPDATE")
    return envelope({"naive": naive, "skip_locked": safe}, tele, [tele_naive])


@router.post("/chaos-crash")
async def chaos_crash():
    """4-step transaction that crashes at step 3; proves Atomicity."""
    pool = db.get_pool()
    log: list = []
    async with pool.acquire() as conn:
        sid = (await enqueue(conn, 1))[0]
        state_before = dict(await conn.fetchrow(
            "SELECT s.status::text AS status, EXISTS(SELECT 1 FROM submission_queue q WHERE q.submission_id = s.submission_id) AS in_queue "
            "FROM submissions s WHERE s.submission_id = $1", sid))
        log.append({"step": 0, "sql": "(setup, committed)", "result": f"submission {sid} created", "state": state_before})
        t0 = time.perf_counter()
        await conn.execute("BEGIN")
        try:
            xid = await conn.fetchval("SELECT pg_current_xact_id()::text")
            log.append({"step": "BEGIN", "sql": "BEGIN;", "result": f"transaction started (xid {xid})"})
            r1 = await conn.execute("UPDATE submissions SET status = 'ACCEPTED' WHERE submission_id = $1", sid)
            log.append({"step": 1, "sql": "UPDATE submissions SET status='ACCEPTED' ...", "result": r1})
            r2 = await conn.execute("DELETE FROM submission_queue WHERE submission_id = $1", sid)
            log.append({"step": 2, "sql": "DELETE FROM submission_queue ...", "result": r2})
            log.append({"step": 3, "sql": "SELECT 1 / 0;   -- deliberate crash before leaderboard update", "result": "executing..."})
            await conn.fetchval("SELECT 1 / 0")
            await conn.execute("CALL sp_record_verdict($1, 100, 0)", sid)       # never reached
            await conn.execute("COMMIT")
            log.append({"step": 4, "sql": "COMMIT;", "result": "UNEXPECTED: committed"})
        except asyncpg.PostgresError as exc:
            log[-1]["result"] = f"ERROR {exc.sqlstate}: {exc}"
            await conn.execute("ROLLBACK")
            log.append({"step": "ROLLBACK", "sql": "ROLLBACK;", "result": "all work of steps 1-2 undone"})
        finally:
            if conn.is_in_transaction():
                await conn.execute("ROLLBACK")
        wall = (time.perf_counter() - t0) * 1000
        after = dict(await conn.fetchrow(
            "SELECT s.status::text AS status, EXISTS(SELECT 1 FROM submission_queue q WHERE q.submission_id = s.submission_id) AS in_queue "
            "FROM submissions s WHERE s.submission_id = $1", sid))
        log.append({"step": "VERIFY", "sql": "SELECT status, in_queue ...", "result": after})
    proven = after["status"] != "ACCEPTED" and after["in_queue"] is True and state_before == after
    tele = make_telemetry("BEGIN; UPDATE submissions...; DELETE FROM submission_queue...; SELECT 1/0; -- crash; ROLLBACK;",
                          [sid], wall, None, "Explicit transaction demo")
    return envelope({"submission_id": sid, "log": log, "atomicity_proven": proven,
                     "before": state_before, "after": after}, tele)


async def _hold_locks(seconds: float) -> None:
    async with db.get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetch("SELECT queue_id FROM submission_queue WHERE locked_by_worker_id IS NULL "
                             "ORDER BY priority DESC, queue_id ASC LIMIT 5 FOR UPDATE")
            await asyncio.sleep(seconds)


@router.get("/lock-telemetry")
async def lock_telemetry():
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, "SELECT * FROM v_lock_telemetry ORDER BY pid, locktype", explain=False)
    return envelope(rows, tele)


@router.post("/lock-demo")
async def lock_demo(seconds: float = 5.0):
    """Holds FOR UPDATE row locks on 5 queue rows for N seconds and shows what pg_locks reveals."""
    seconds = max(1.0, min(seconds, 15.0))
    async with db.get_pool().acquire() as conn:
        pending = await conn.fetchval("SELECT count(*) FROM submission_queue WHERE locked_by_worker_id IS NULL")
        if pending < 5:
            await enqueue(conn, 10)
    task = asyncio.create_task(_hold_locks(seconds))
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.sleep(0.5)
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, "SELECT * FROM v_lock_telemetry ORDER BY pid, locktype", explain=False)
    return envelope({"holding_seconds": seconds, "locks": rows}, tele)


@router.post("/reset")
async def reset():
    async with db.get_pool().acquire() as conn:
        status, tele = await db.xray_execute(conn, "DELETE FROM submissions WHERE status IN ('QUEUED','PROCESSING')")
    return envelope({"status": status}, tele)
