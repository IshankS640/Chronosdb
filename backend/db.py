"""asyncpg pool + the 'DBA X-Ray' telemetry wrapper. No ORM anywhere."""
import json
import os
import time
from typing import Any, Optional

import asyncpg

DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/chronosdb")
_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # json/jsonb <-> python objects (dict/list), so EXPLAIN (FORMAT JSON) arrives already parsed
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DSN, min_size=4, max_size=20, init=_init_connection)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


# ---------------------------------------------------------------- X-Ray
def _walk(node: dict, acc: list) -> None:
    acc.append(node)
    for child in node.get("Plans", []) or []:
        _walk(child, acc)


def summarize_plan(plan_json: Any) -> dict:
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    root = plan_json[0]
    top = root["Plan"]
    nodes: list = []
    _walk(top, nodes)
    scans = []
    for n in nodes:
        if "Scan" in n.get("Node Type", ""):
            label = n["Node Type"]
            if "Index Name" in n:
                label += f" using {n['Index Name']}"
            elif "Relation Name" in n:
                label += f" on {n['Relation Name']}"
            scans.append(label)
    return {
        "execution_time_ms": root.get("Execution Time"),
        "planning_time_ms": root.get("Planning Time"),
        "scan_type": " > ".join(scans) if scans else top.get("Node Type", "n/a"),
        "buffers_shared_hit": top.get("Shared Hit Blocks", 0),
        "buffers_shared_read": top.get("Shared Read Blocks", 0),
        "explain_plan": plan_json,
    }


def make_telemetry(raw_sql: str, params: Optional[list] = None, wall_ms: Optional[float] = None,
                   plan_json: Any = None, note: Optional[str] = None) -> dict:
    t = {
        "raw_sql": raw_sql.strip(),
        "params": [str(p) for p in (params or [])],
        "execution_time_ms": round(wall_ms, 3) if wall_ms is not None else None,
        "wall_time_ms": round(wall_ms, 3) if wall_ms is not None else None,
        "planning_time_ms": None,
        "explain_plan": [],
        "scan_type": "n/a",
        "buffers_shared_hit": 0,
        "buffers_shared_read": 0,
        "note": note,
    }
    if plan_json is not None:
        s = summarize_plan(plan_json)
        t.update(s)
        if t["execution_time_ms"] is not None:
            t["execution_time_ms"] = round(t["execution_time_ms"], 3)
    return t


async def xray_fetch(conn: asyncpg.Connection, sql: str, *args, explain: bool = True):
    """Run a READ-ONLY query. Returns (rows_as_dicts, telemetry).
    EXPLAIN ANALYZE executes the statement a second time -> only use explain=True for SELECTs."""
    t0 = time.perf_counter()
    rows = await conn.fetch(sql, *args)
    wall = (time.perf_counter() - t0) * 1000
    plan = None
    note = None
    if explain:
        try:
            plan = await conn.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, *args)
        except asyncpg.PostgresError as exc:           # never let telemetry break the API
            note = f"EXPLAIN failed: {exc}"
    else:
        note = "EXPLAIN skipped (write path or procedure call)"
    return [dict(r) for r in rows], make_telemetry(sql, list(args), wall, plan, note)


async def xray_execute(conn: asyncpg.Connection, sql: str, *args):
    """Run a write / CALL. Returns (status_string, telemetry). Never EXPLAIN-ANALYZEd."""
    t0 = time.perf_counter()
    status = await conn.execute(sql, *args)
    wall = (time.perf_counter() - t0) * 1000
    return status, make_telemetry(sql, list(args), wall, None, "EXPLAIN skipped (write path or procedure call)")


def envelope(data: Any, telemetry: dict, extra: Optional[list] = None) -> dict:
    return {"data": data, "xray_telemetry": telemetry, "xray_extra": extra or []}
