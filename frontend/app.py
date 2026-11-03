"""ChronosDB dashboard (Streamlit). Run:  streamlit run app.py   (API must be running on :8000)"""
import datetime as dt
import os

import pandas as pd
import requests
import streamlit as st

API = os.getenv("CHRONOS_API", "http://localhost:8000")
if not API.startswith("http://") and not API.startswith("https://"):
    API = f"http://{API}"
CONTEST_ID = 1
ss = st.session_state

st.set_page_config(page_title="ChronosDB", page_icon="⏳", layout="wide")
for key, default in [("xray", None), ("xray_extra", []), ("xray_history", []), ("live", None), ("timeline", None),
                     ("tt_last", None), ("tt_rows", None), ("tt_prev", {}), ("alerts", None), ("workers", None),
                     ("conc", None), ("crash", None), ("locks", None), ("bench", {}), ("mv", None), ("mv_refresh", None),
                     ("tamper", None), ("verdict", None), ("chain", None), ("tail", None), ("stats", None)]:
    ss.setdefault(key, default)


def call(method, path, params=None, body=None, record=True):
    """Calls the API, unwraps the envelope, pushes telemetry into the X-Ray drawer."""
    try:
        r = requests.request(method, API + path, params=params, json=body, timeout=300)
    except requests.RequestException as exc:
        st.error(f"Cannot reach the API at {API}. Is uvicorn running?  ({exc})")
        return None
    if r.status_code >= 400:
        st.error(f"API error {r.status_code}: {r.text[:600]}")
        return None
    payload = r.json()
    if record:
        ss.xray = payload.get("xray_telemetry")
        ss.xray_extra = payload.get("xray_extra", [])
        if ss.xray:
            ss.xray_history = ([{"t": dt.datetime.now().strftime("%H:%M:%S"), "sql": ss.xray["raw_sql"][:90].replace("\n", " "),
                                 "ms": ss.xray.get("execution_time_ms")}] + ss.xray_history)[:10]
    return payload["data"]


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)


def plan_lines(node, depth=0, out=None):
    out = [] if out is None else out
    idx = f" using {node['Index Name']}" if "Index Name" in node else ""
    rel = f" on {node['Relation Name']}" if "Relation Name" in node else ""
    out.append("   " * depth + ("└─ " if depth else "") +
               f"{node['Node Type']}{idx}{rel}  [actual {node.get('Actual Total Time', '?')} ms, rows {node.get('Actual Rows', '?')}, buffers hit {node.get('Shared Hit Blocks', 0)}]")
    for ch in node.get("Plans", []) or []:
        plan_lines(ch, depth + 1, out)
    return out


def render_xray_item(t, title):
    st.markdown(f"**{title}**")
    c1, c2 = st.columns(2)
    c1.metric("Execution", f"{t['execution_time_ms']} ms" if t.get("execution_time_ms") is not None else "n/a")
    c2.metric("Buffers hit", t.get("buffers_shared_hit", 0))
    st.caption(f"Scan: {t.get('scan_type', 'n/a')}")
    st.code(t["raw_sql"], language="sql")
    if t.get("params"):
        st.caption("Params: " + ", ".join(t["params"]))
    if t.get("note"):
        st.caption(f"ℹ️ {t['note']}")
    if t.get("explain_plan"):
        with st.expander("Query plan tree"):
            st.code("\n".join(plan_lines(t["explain_plan"][0]["Plan"])), language="text")
        with st.expander("Raw EXPLAIN JSON"):
            st.json(t["explain_plan"])


main_col, xray_col = st.columns([3, 1.25])

# =============================================================== MAIN
with main_col:
    st.title("⏳ ChronosDB")
    st.caption("Self-healing, time-travel engine for competitive coding  |  PostgreSQL · asyncpg · FastAPI · no ORM")
    tabs = st.tabs(["1 · Live & Time-Travel", "2 · Self-Healing Lab", "3 · Concurrency Lab", "4 · Benchmark Lab"])

    # ------------------------------------------------------- TAB 1
    with tabs[0]:
        if ss.timeline is None:
            ss.timeline = call("GET", "/api/healing/timeline", {"contest_id": CONTEST_ID}, record=False)
        if ss.live is None:
            ss.live = call("GET", "/api/healing/leaderboard", {"contest_id": CONTEST_ID, "limit": 15})
        st.subheader("Live leaderboard (table: leaderboard_scores)")
        if st.button("Refresh live leaderboard"):
            ss.live = call("GET", "/api/healing/leaderboard", {"contest_id": CONTEST_ID, "limit": 15})
        if ss.live:
            st.dataframe(pd.DataFrame(ss.live)[["rank_pos", "handle", "total_points", "total_penalty_sec"]], hide_index=True)

        st.subheader("Time-travel scoreboard (reconstructed from the audit ledger)")
        tl = ss.timeline
        if tl:
            start, end = parse_ts(tl["start_time"]), parse_ts(tl["end_time"])
            st.caption(f"{tl['title']} · {tl['ledger_events']:,} ledger events · times in UTC")
            t = st.slider("Drag to travel in time", min_value=start, max_value=end, value=end,
                          step=dt.timedelta(seconds=1), format="YYYY-MM-DD HH:mm:ss", key="tt_slider")
            if ss.tt_last != t:
                rows = call("GET", "/api/healing/time-travel",
                            {"timestamp": t.replace(tzinfo=dt.timezone.utc).isoformat(), "contest_id": CONTEST_ID, "limit": 15})
                if rows is not None:
                    ss.tt_prev = {r["user_id"]: r["rank_pos"] for r in (ss.tt_rows or [])}
                    ss.tt_rows, ss.tt_last = rows, t
            if ss.tt_rows:
                df = pd.DataFrame(ss.tt_rows)
                df["Δ rank"] = [ss.tt_prev.get(u, r) - r for u, r in zip(df["user_id"], df["rank_pos"])]
                st.dataframe(df[["rank_pos", "handle", "points_at_time", "penalty_at_time", "Δ rank"]], hide_index=True)
                st.bar_chart(df.set_index("handle")["points_at_time"])
                st.caption("Δ rank > 0 means the user climbed since the previous slider position. "
                           "At the far right the table equals the live leaderboard.")

    # ------------------------------------------------------- TAB 2
    with tabs[1]:
        st.subheader("Tamper Defense Lab")
        if ss.live is None:
            ss.live = call("GET", "/api/healing/leaderboard", {"contest_id": CONTEST_ID, "limit": 15})
        if ss.live:
            labels = {f"{r['handle']} (id {r['user_id']}) — {r['total_points']} pts": r["user_id"] for r in ss.live}
            choice = st.selectbox("Victim account", list(labels.keys()))
            uid = labels[choice]
            c1, c2 = st.columns(2)
            fake_pts = c1.number_input("Fake score", min_value=0, value=99999, step=1000)
            action = c2.radio("Attack type", ["update", "delete"], horizontal=True)
            if st.button("🔥 Execute Direct SQL Tamper"):
                ss.tamper = call("POST", "/api/healing/tamper",
                                 body={"contest_id": CONTEST_ID, "user_id": uid, "fake_points": int(fake_pts), "action": action})
                ss.alerts = call("GET", "/api/healing/alerts", {"limit": 10}, record=False)
                ss.live = call("GET", "/api/healing/leaderboard", {"contest_id": CONTEST_ID, "limit": 15}, record=False)
            if ss.tamper:
                tp = ss.tamper
                st.code(tp["tamper_sql"], language="sql")
                a, b = st.columns(2)
                a.metric("Score BEFORE attack", tp["before"]["total_points"])
                b.metric("Score AFTER attack", tp["after"]["total_points"] if tp["after"] else "row missing")
                if tp["healed"]:
                    st.success("Trigger trg_self_healing_leaderboard silently restored the true score.")
                else:
                    st.error("Score changed — healing did NOT work. Check 03_self_healing_triggers.sql.")
                if tp["latest_alerts"]:
                    al = tp["latest_alerts"][0]
                    st.error(f"🚨 INTRUSION ALERT #{al['alert_id']} · db_user={al['db_user']} · ip={al['client_addr']} · "
                             f"attempted={al['attempted_points']} · restored={al['restored_points']}\n\n{al['incident_details']}")

            st.divider()
            st.markdown("**Legitimate path:** record a verdict through `sp_record_verdict` (grows the audit chain)")
            v1, v2 = st.columns(2)
            vp = v1.number_input("Points to add", min_value=0, value=100, step=50)
            vpen = v2.number_input("Penalty seconds to add", min_value=0, value=300, step=60)
            if st.button("✅ Record legitimate verdict"):
                ss.verdict = call("POST", "/api/healing/judge-verdict",
                                  body={"contest_id": CONTEST_ID, "user_id": uid, "points": int(vp), "penalty": int(vpen)})
                ss.live = call("GET", "/api/healing/leaderboard", {"contest_id": CONTEST_ID, "limit": 15}, record=False)
            if ss.verdict:
                st.success(f"Score {ss.verdict['before']['total_points']} → {ss.verdict['after']['total_points']}  (new ledger events below)")
                st.dataframe(pd.DataFrame(ss.verdict["ledger_tail"]), hide_index=True)

        st.divider()
        v, w = st.columns(2)
        if v.button("🔗 Verify SHA-256 Audit Chain"):
            ss.chain = call("GET", "/api/healing/verify-chain")
        if w.button("Refresh intrusion alerts"):
            ss.alerts = call("GET", "/api/healing/alerts", {"limit": 10}, record=False)
        if ss.chain:
            (st.success if ss.chain["is_valid"] else st.error)(
                f"{'✅ VERIFIED' if ss.chain['is_valid'] else '❌ BROKEN'} — {ss.chain['total_events']:,} events checked. {ss.chain['message']}")
        st.markdown("**Intrusion alerts feed**")
        if ss.alerts:
            st.dataframe(pd.DataFrame(ss.alerts)[["alert_id", "detected_at", "user_id", "attempted_points", "restored_points", "db_user", "client_addr"]], hide_index=True)
        elif ss.alerts is not None:
            st.info("No intrusion attempts recorded yet.")

    # ------------------------------------------------------- TAB 3
    with tabs[2]:
        st.subheader("Worker swarm (8 judge nodes)")
        if ss.workers is None:
            ss.workers = call("GET", "/api/concurrency/workers", record=False)
        if st.button("Refresh swarm"):
            ss.workers = call("GET", "/api/concurrency/workers")
        if ss.workers:
            cols = st.columns(4)
            for i, w_ in enumerate(ss.workers["workers"]):
                icon = {"IDLE": "🟢", "BUSY": "🟠", "OFFLINE": "🔴"}.get(w_["status"], "⚪")
                cols[i % 4].metric(w_["worker_name"], f"{icon} {w_['status']}", f"{w_['processed_count']} jobs done", delta_color="off")
            q = ss.workers["queue"]
            st.caption(f"Queue depth — pending: {q['pending']} · locked: {q['locked']}")

        st.subheader("Naive vs FOR UPDATE SKIP LOCKED")
        if st.button("⚔️ Fire 100 concurrent submissions (both modes)"):
            ss.conc = call("POST", "/api/concurrency/fire-compare", {"n": 100})
            ss.workers = call("GET", "/api/concurrency/workers", record=False)
        if ss.conc:
            n_, s_ = ss.conc["naive"], ss.conc["skip_locked"]
            a, b = st.columns(2)
            a.metric("Naive collisions", n_["collisions"], f"{n_['total_claims']} claims for {n_['jobs_enqueued']} jobs", delta_color="off")
            b.metric("SKIP LOCKED collisions", s_["collisions"], f"{s_['total_claims']} claims for {s_['jobs_enqueued']} jobs", delta_color="off")
            st.bar_chart(pd.DataFrame({"collisions": [n_["collisions"], s_["collisions"]]}, index=["Naive", "SKIP LOCKED"]))
            st.caption(f"Swarm time — naive {n_['duration_ms']} ms · SKIP LOCKED {s_['duration_ms']} ms. Collision counts vary run to run (it is a real race).")

        st.subheader("ACID atomicity proof")
        if st.button("💥 Inject mid-transaction crash"):
            ss.crash = call("POST", "/api/concurrency/chaos-crash")
        if ss.crash:
            for e in ss.crash["log"]:
                st.code(f"[{e['step']}] {e['sql']}\n    → {e['result']}", language="sql")
            (st.success if ss.crash["atomicity_proven"] else st.error)(
                "ATOMICITY PROVEN: steps 1–2 were fully rolled back." if ss.crash["atomicity_proven"] else "Atomicity check FAILED")

        st.subheader("Live lock telemetry")
        l1, l2 = st.columns(2)
        if l1.button("🔒 Hold row locks 5 s & show pg_locks"):
            ss.locks = call("POST", "/api/concurrency/lock-demo", {"seconds": 5})
        if l2.button("Reset demo queue"):
            call("POST", "/api/concurrency/reset")
            ss.workers = call("GET", "/api/concurrency/workers", record=False)
        if ss.locks:
            st.caption("Row locks live in tuple headers; pg_locks shows the table lock + transaction-id lock of the holder.")
            st.dataframe(pd.DataFrame(ss.locks["locks"]), hide_index=True)

    # ------------------------------------------------------- TAB 4
    with tabs[3]:
        st.subheader("100k-row index benchmarks (planner flags: Seq Scan vs Index)")
        if ss.stats is None:
            ss.stats = call("GET", "/api/benchmark/table-stats", record=False)
        if ss.stats:
            st.dataframe(pd.DataFrame(ss.stats).head(6), hide_index=True)
        for key, title, path in [("plag", "Benchmark 1 · Plagiarism hash lookup (composite B-Tree)", "/api/benchmark/plagiarism"),
                                 ("gin", "Benchmark 2 · JSONB AST-tag search (GIN)", "/api/benchmark/jsonb-gin"),
                                 ("tt", "Benchmark 3 · Single-user time-travel lookup (temporal B-Tree)", "/api/benchmark/time-travel-lookup")]:
            if st.button(f"▶ Run {title.split(' · ')[0]}", key=f"btn_{key}"):
                ss.bench[key] = call("GET", path)
            res = ss.bench.get(key)
            if res:
                st.markdown(f"**{res['benchmark']}**")
                a, b, c = st.columns(3)
                a.metric("Sequential scan", f"{res['sequential_ms']} ms")
                b.metric("Indexed", f"{res['indexed_ms']} ms")
                c.metric("Speed-up", f"{res['speedup_x']}x" if res["speedup_x"] else "n/a")
                st.caption(f"{res['rows_returned']} rows · seq: {res['sequential_scan_type']} · indexed: {res['indexed_scan_type']}")
                if not res["forced_seq_ok"] or not res["indexed_used_index"]:
                    st.warning("The planner did not use the expected scan type — see the troubleshooting section of the guide.")

        st.subheader("Materialized view lab")
        m1, m2 = st.columns(2)
        if m1.button("⚖️ Raw 5-table join vs mv_contest_analytics"):
            ss.mv = call("GET", "/api/benchmark/mv-compare", {"contest_id": CONTEST_ID})
        if m2.button("♻️ REFRESH ... CONCURRENTLY (while reading)"):
            ss.mv_refresh = call("POST", "/api/benchmark/mv-refresh", {"contest_id": CONTEST_ID})
        if ss.mv:
            a, b, c = st.columns(3)
            a.metric("Live join", f"{ss.mv['raw_join_ms']} ms")
            b.metric("Materialized view", f"{ss.mv['materialized_view_ms']} ms")
            c.metric("Speed-up", f"{ss.mv['speedup_x']}x" if ss.mv["speedup_x"] else "n/a")
            st.dataframe(pd.DataFrame(ss.mv["rows"]).head(12), hide_index=True)
        if ss.mv_refresh:
            r = ss.mv_refresh
            st.info(f"Refresh took {r['refresh_ms']} ms. {r['reads_during_refresh']} reads ran DURING the refresh; "
                    f"slowest read {r['slowest_read_ms']} ms. Reads blocked: {r['reads_blocked']}")

# =============================================================== X-RAY DRAWER
with xray_col:
    st.header("🩻 DBA X-Ray")
    if ss.xray:
        render_xray_item(ss.xray, "Last query")
        for i, extra in enumerate(ss.xray_extra):
            render_xray_item(extra, f"Related query {i + 1}")
    else:
        st.info("Click any button — the SQL, timing, buffer hits and query plan appear here.")
    if ss.xray_history:
        with st.expander("History (last 10)"):
            st.dataframe(pd.DataFrame(ss.xray_history), hide_index=True)
