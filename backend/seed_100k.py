"""Generates 2,000 users, 5 contests, 40 problems, 100,000 submissions, 25,000 hash-chained ledger events.
Usage (from the backend/ folder):   python seed_100k.py            (fresh database)
                                    python seed_100k.py --force    (wipe & re-seed)"""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import sys
import time

import asyncpg
from faker import Faker

from constants import GENESIS, PLAGIARISM_HASH, RARE_TAG, SECRET

DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/chronosdb")
UTC = dt.timezone.utc
N_USERS, N_SUBMISSIONS, EVENTS_PER_USER, C1_USERS = 2000, 100_000, 50, 500   # 500 x 50 = 25,000 ledger events

random.seed(42)
Faker.seed(42)
fake = Faker()

CONTEST_STARTS = [dt.datetime(2026, 3, 15, 10, 0, tzinfo=UTC), dt.datetime(2025, 9, 6, 9, 0, tzinfo=UTC),
                  dt.datetime(2025, 10, 18, 9, 0, tzinfo=UTC), dt.datetime(2025, 11, 22, 9, 0, tzinfo=UTC),
                  dt.datetime(2026, 1, 10, 9, 0, tzinfo=UTC)]
CONTEST_TITLES = ["ChronosDB Live Cup 2026", "Autumn Algorithm Open", "Graph Theory Gauntlet",
                  "Winter Bug-Bounty Blitz", "New Year Code Sprint"]
POINTS = [100, 200, 300, 400, 500, 600, 800, 1000]
LANGS, LANG_W = ["CPP17", "PYTHON3", "JAVA", "RUST"], [45, 30, 15, 10]
STATUSES, STATUS_W = ["ACCEPTED", "WRONG_ANSWER", "TIME_LIMIT_EXCEEDED", "SYSTEM_ERROR"], [38, 42, 17, 3]
TAGS = ["dp", "graph", "greedy", "bitmask", "segment_tree", "dsu", "binary_search", "math", "strings", "geometry", "flows", "trees"]


def rhash() -> str:
    return hashlib.sha256(random.getrandbits(128).to_bytes(16, "big")).hexdigest()


def event_hash(prev: str, c: int, u: int, pts: int, pen: int, ts: dt.datetime) -> str:
    # MUST match fn_compute_event_hash() in 03_self_healing_triggers.sql
    s = f"{prev}|{c}|{u}|{pts}|{pen}|{ts.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')}"
    return hashlib.sha256(s.encode()).hexdigest()


def signature(c: int, u: int, pts: int, pen: int) -> str:
    # MUST match fn_compute_signature()
    return hmac.new(SECRET.encode(), f"{c}|{u}|{pts}|{pen}".encode(), hashlib.sha256).hexdigest()


async def main(force: bool) -> None:
    conn = await asyncpg.connect(DSN)
    await conn.set_type_codec("jsonb", encoder=lambda v: b"\x01" + json.dumps(v).encode("utf-8"), decoder=lambda b: json.loads(b[1:].decode("utf-8")), schema="pg_catalog", format="binary")
    existing = await conn.fetchval("SELECT count(*) FROM users")
    if existing and not force:
        print("Database already has data. Use:  python seed_100k.py --force")
        sys.exit(1)
    T0 = time.perf_counter()
    await conn.execute("ALTER TABLE leaderboard_scores DISABLE TRIGGER USER")
    await conn.execute("ALTER TABLE score_audit_ledger DISABLE TRIGGER USER")
    try:
        if existing:
            print("Wiping existing data ...")
            await conn.execute("""TRUNCATE intrusion_alerts, submission_queue, score_audit_ledger, leaderboard_scores,
                                  submissions, test_cases, contest_registrations, problems, contests, judge_workers, users
                                  RESTART IDENTITY CASCADE""")

        # ---- users
        users = []
        for i in range(1, N_USERS + 1):
            role = "JUDGE_ADMIN" if i == 1 else "AUDITOR" if i == 2 else "CONTESTANT"
            users.append((i, f"{fake.user_name()[:38]}_{i}", f"user{i}@chronos.example", role, random.randint(800, 2800),
                          dt.datetime(2025, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=random.randint(0, 365 * 86400))))
        await conn.copy_records_to_table("users", records=users, columns=["user_id", "handle", "email", "role", "rating", "created_at"])
        contestants = list(range(3, N_USERS + 1))

        # ---- contests, problems, test cases
        contests = [(i + 1, CONTEST_TITLES[i], CONTEST_STARTS[i], CONTEST_STARTS[i] + dt.timedelta(hours=4), i == 0) for i in range(5)]
        await conn.copy_records_to_table("contests", records=contests, columns=["contest_id", "title", "start_time", "end_time", "is_active"])
        problems, tcases = [], []
        for c in range(1, 6):
            for k in range(8):
                pid = (c - 1) * 8 + k + 1
                problems.append((pid, c, "ABCDEFGH"[k], f"{fake.word().title()} {fake.word().title()}", POINTS[k],
                                 random.choice([1000, 2000, 3000, 4000]), 262144))
                for t in range(5):
                    tcases.append((pid, rhash(), rhash(), t == 0))
        await conn.copy_records_to_table("problems", records=problems,
                                         columns=["problem_id", "contest_id", "code", "title", "points", "time_limit_ms", "memory_limit_kb"])
        await conn.copy_records_to_table("test_cases", records=tcases,
                                         columns=["problem_id", "input_hash", "expected_output_hash", "is_sample"])

        # ---- registrations
        reg = {1: random.sample(contestants, C1_USERS)}
        for c in range(2, 6):
            reg[c] = random.sample(contestants, random.randint(600, 1200))
        regs = [(c, u, CONTEST_STARTS[c - 1] - dt.timedelta(days=random.randint(1, 20))) for c, us in reg.items() for u in us]
        await conn.copy_records_to_table("contest_registrations", records=regs, columns=["contest_id", "user_id", "registered_at"])

        # ---- judge workers
        await conn.copy_records_to_table("judge_workers", records=[(i, f"Judge-Node-{i:02d}", "IDLE", dt.datetime.now(UTC), 0) for i in range(1, 9)],
                                         columns=["worker_id", "worker_name", "status", "last_heartbeat", "processed_count"])
        print(f"  users/contests/problems/registrations done  ({time.perf_counter() - T0:.1f}s)")

        # ---- 100,000 submissions
        dup_pool = [rhash() for _ in range(5000)]
        subs = []
        for i in range(N_SUBMISSIONS):
            planted = i < 40                                          # 40 copies of one plagiarised solution on problem 1
            c = 1 if planted else random.choices([1, 2, 3, 4, 5], weights=[30, 17.5, 17.5, 17.5, 17.5])[0]
            pid = 1 if planted else (c - 1) * 8 + random.randint(1, 8)
            uid = random.choice(reg[c])
            h = PLAGIARISM_HASH if planted else (random.choice(dup_pool) if random.random() < 0.15 else rhash())
            tags = random.sample(TAGS, random.randint(1, 3))
            if random.random() < 0.005:
                tags.append(RARE_TAG)
            meta = {"ast_tags": tags, "cyclomatic_complexity": random.randint(1, 40)}
            st = random.choices(STATUSES, weights=STATUS_W)[0]
            ex = None if st == "SYSTEM_ERROR" else (2000 if st == "TIME_LIMIT_EXCEEDED" else random.randint(15, 1900))
            ts = CONTEST_STARTS[c - 1] + dt.timedelta(seconds=random.uniform(0, 4 * 3600))
            subs.append((pid, uid, random.choices(LANGS, weights=LANG_W)[0], h, meta, st, ex, ts))
        subs.sort(key=lambda r: r[7])
        subs = [(i + 1, *r) for i, r in enumerate(subs)]
        await conn.copy_records_to_table("submissions", records=subs,
            columns=["submission_id", "problem_id", "user_id", "language", "source_code_hash", "code_metadata", "status", "execution_time_ms", "submitted_at"])
        print(f"  {len(subs):,} submissions done  ({time.perf_counter() - T0:.1f}s)")

        # ---- 25,000 hash-chained ledger events + matching leaderboard
        events = sorted(((CONTEST_STARTS[0] + dt.timedelta(seconds=random.uniform(0, 4 * 3600)), u)
                         for u in reg[1] for _ in range(EVENTS_PER_USER)), key=lambda e: e[0])
        prev, state, last_ts, ledger = GENESIS, {}, {}, []
        for ev_id, (ts, u) in enumerate(events, start=1):
            op, ope = state.get(u, (0, 0))
            np_, npe = op + random.choice([50, 100, 150, 200]), ope + random.randint(60, 1500)
            h = event_hash(prev, 1, u, np_, npe, ts)
            ledger.append((ev_id, 1, u, None, op, np_, ope, npe, ts, prev, h))
            prev, state[u], last_ts[u] = h, (np_, npe), ts
        await conn.copy_records_to_table("score_audit_ledger", records=ledger,
            columns=["event_id", "contest_id", "user_id", "submission_id", "old_points", "new_points", "old_penalty_sec",
                     "new_penalty_sec", "recorded_at", "prev_event_hash", "current_event_hash"])
        board = [(1, u, p, pen, last_ts[u], signature(1, u, p, pen)) for u, (p, pen) in state.items()]
        await conn.copy_records_to_table("leaderboard_scores", records=board,
            columns=["contest_id", "user_id", "total_points", "total_penalty_sec", "last_updated", "integrity_signature"])
        print(f"  {len(ledger):,} ledger events + {len(board)} leaderboard rows done  ({time.perf_counter() - T0:.1f}s)")
    finally:
        await conn.execute("ALTER TABLE leaderboard_scores ENABLE TRIGGER USER")
        await conn.execute("ALTER TABLE score_audit_ledger ENABLE TRIGGER USER")

    # sequences must continue after our explicit ids
    for tbl, col in [("users", "user_id"), ("contests", "contest_id"), ("problems", "problem_id"),
                     ("judge_workers", "worker_id"), ("submissions", "submission_id"), ("score_audit_ledger", "event_id")]:
        await conn.execute(f"SELECT setval(pg_get_serial_sequence('{tbl}', '{col}'), (SELECT COALESCE(MAX({col}), 1) FROM {tbl}))")

    await conn.execute("REFRESH MATERIALIZED VIEW mv_contest_analytics")
    await conn.execute("REFRESH MATERIALIZED VIEW mv_top_performers")
    await conn.execute("VACUUM ANALYZE")
    for t in ["users", "contests", "problems", "test_cases", "contest_registrations", "judge_workers", "submissions",
              "leaderboard_scores", "score_audit_ledger"]:
        print(f"  {t:24s} {await conn.fetchval(f'SELECT count(*) FROM {t}'):>9,}")
    chain = await conn.fetchrow("SELECT * FROM fn_verify_ledger_chain()")
    print(f"Chain verification: valid={chain['is_valid']}  events={chain['total_events']:,}")
    print(f"DONE in {time.perf_counter() - T0:.1f}s")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main("--force" in sys.argv))
