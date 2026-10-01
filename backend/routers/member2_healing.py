"""Member 2: leaderboard, tamper simulation, intrusion alerts, chain verification, time travel."""
import datetime as dt
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
from db import envelope, make_telemetry, xray_execute, xray_fetch

router = APIRouter(prefix="/api/healing", tags=["member2-healing"])

ROW_SQL = ("SELECT contest_id, user_id, total_points, total_penalty_sec, last_updated "
           "FROM leaderboard_scores WHERE contest_id = $1 AND user_id = $2")


class TamperRequest(BaseModel):
    contest_id: int = 1
    user_id: int
    fake_points: int = 99999
    action: Literal["update", "delete"] = "update"


class VerdictRequest(BaseModel):
    contest_id: int = 1
    user_id: int
    points: int = 100
    penalty: int = 300


@router.get("/timeline")
async def timeline(contest_id: int = 1):
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, """
            SELECT c.contest_id, c.title, c.start_time, c.end_time,
                   (SELECT count(*) FROM score_audit_ledger l WHERE l.contest_id = c.contest_id) AS ledger_events
            FROM contests c WHERE c.contest_id = $1""", contest_id)
    if not rows:
        raise HTTPException(404, "contest not found")
    return envelope(rows[0], tele)


@router.get("/leaderboard")
async def leaderboard(contest_id: int = 1, limit: int = 50):
    limit = max(1, min(limit, 500))
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, """
            SELECT RANK() OVER (ORDER BY ls.total_points DESC, ls.total_penalty_sec ASC) AS rank_pos,
                   ls.user_id, u.handle, ls.total_points, ls.total_penalty_sec, ls.last_updated
            FROM leaderboard_scores ls
            JOIN users u ON u.user_id = ls.user_id
            WHERE ls.contest_id = $1
            ORDER BY rank_pos, u.handle
            LIMIT $2""", contest_id, limit)
    return envelope(rows, tele)


@router.get("/time-travel")
async def time_travel(timestamp: dt.datetime, contest_id: int = 1, limit: int = 50):
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    limit = max(1, min(limit, 500))
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn,
            "SELECT * FROM fn_reconstruct_leaderboard_at($1, $2) LIMIT $3", contest_id, timestamp, limit)
    return envelope(rows, tele)


@router.get("/alerts")
async def alerts(limit: int = 20):
    limit = max(1, min(limit, 200))
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, """
            SELECT alert_id, target_table, contest_id, user_id, attempted_points, restored_points,
                   db_user, client_addr, detected_at, incident_details
            FROM intrusion_alerts ORDER BY alert_id DESC LIMIT $1""", limit)
    return envelope(rows, tele)


@router.get("/ledger-tail")
async def ledger_tail(limit: int = 10):
    limit = max(1, min(limit, 100))
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, """
            SELECT event_id, contest_id, user_id, old_points, new_points, new_penalty_sec, recorded_at,
                   left(prev_event_hash, 12) AS prev_hash, left(current_event_hash, 12) AS hash
            FROM score_audit_ledger ORDER BY event_id DESC LIMIT $1""", limit)
    return envelope(rows, tele)


@router.get("/verify-chain")
async def verify_chain():
    async with db.get_pool().acquire() as conn:
        rows, tele = await xray_fetch(conn, "SELECT * FROM fn_verify_ledger_chain()")
    return envelope(rows[0], tele)


@router.post("/tamper")
async def tamper(req: TamperRequest):
    """Runs a raw, UNAUTHORIZED statement exactly like a rogue admin in pgAdmin would."""
    async with db.get_pool().acquire() as conn:
        before = await conn.fetchrow(ROW_SQL, req.contest_id, req.user_id)
        if before is None:
            raise HTTPException(404, "that user has no leaderboard row in this contest")
        if req.action == "update":
            sql = "UPDATE leaderboard_scores SET total_points = $3 WHERE contest_id = $1 AND user_id = $2"
            args = (req.contest_id, req.user_id, req.fake_points)
        else:
            sql = "DELETE FROM leaderboard_scores WHERE contest_id = $1 AND user_id = $2"
            args = (req.contest_id, req.user_id)
        status, tele = await xray_execute(conn, sql, *args)
        after = await conn.fetchrow(ROW_SQL, req.contest_id, req.user_id)
        new_alerts = await conn.fetch("""SELECT alert_id, attempted_points, restored_points, db_user, client_addr,
                                                detected_at, incident_details
                                         FROM intrusion_alerts ORDER BY alert_id DESC LIMIT 3""")
    healed = after is not None and after["total_points"] == before["total_points"]
    return envelope({
        "tamper_sql": sql.replace("$1", str(req.contest_id)).replace("$2", str(req.user_id)).replace("$3", str(req.fake_points)),
        "statement_status": status,
        "before": dict(before), "after": dict(after) if after else None,
        "healed": healed, "latest_alerts": [dict(a) for a in new_alerts],
    }, tele)


@router.post("/judge-verdict")
async def judge_verdict(req: VerdictRequest):
    """LEGITIMATE score change through the stored procedure."""
    async with db.get_pool().acquire() as conn:
        sid = await conn.fetchval("""SELECT s.submission_id FROM submissions s
                                     JOIN problems p ON p.problem_id = s.problem_id
                                     WHERE p.contest_id = $1 AND s.user_id = $2
                                     ORDER BY s.submission_id LIMIT 1""", req.contest_id, req.user_id)
        if sid is None:
            raise HTTPException(404, "that user has no submission in this contest")
        before = await conn.fetchrow(ROW_SQL, req.contest_id, req.user_id)
        _, tele = await xray_execute(conn, "CALL sp_record_verdict($1, $2, $3)", sid, req.points, req.penalty)
        after = await conn.fetchrow(ROW_SQL, req.contest_id, req.user_id)
        tail = await conn.fetch("""SELECT event_id, old_points, new_points, left(prev_event_hash, 12) AS prev_hash,
                                          left(current_event_hash, 12) AS hash
                                   FROM score_audit_ledger ORDER BY event_id DESC LIMIT 3""")
    return envelope({"submission_id": sid, "before": dict(before) if before else None,
                     "after": dict(after), "ledger_tail": [dict(t) for t in tail]}, tele)
