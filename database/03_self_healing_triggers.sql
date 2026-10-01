-- =====================================================================
-- ChronosDB | 03_self_healing_triggers.sql
-- Authorization token, self-healing trigger, hash-chained ledger, verifier
-- =====================================================================

-- ---------- helpers ------------------------------------------------
CREATE OR REPLACE FUNCTION fn_genesis_hash() RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$ SELECT repeat('0', 64) $$;

-- Timestamp is rendered in UTC with microseconds, so the hash never depends on
-- the session TimeZone setting. Python seed uses the IDENTICAL formula.
CREATE OR REPLACE FUNCTION fn_compute_event_hash(
    p_prev TEXT, p_contest INT, p_user INT, p_points INT, p_penalty INT, p_ts TIMESTAMPTZ)
RETURNS TEXT LANGUAGE sql STABLE AS $$
    SELECT encode(digest(
        p_prev || '|' || p_contest::text || '|' || p_user::text || '|' ||
        p_points::text || '|' || p_penalty::text || '|' ||
        to_char(p_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        'sha256'), 'hex')
$$;

CREATE OR REPLACE FUNCTION fn_compute_signature(p_contest INT, p_user INT, p_points INT, p_penalty INT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(hmac(
        p_contest::text || '|' || p_user::text || '|' || p_points::text || '|' || p_penalty::text,
        'CHRONOS_MASTER_SECRET_2026', 'sha256'), 'hex')
$$;

-- TRUE only if the transaction-local token matches HMAC(submission_id, secret).
CREATE OR REPLACE FUNCTION fn_is_authorized() RETURNS BOOLEAN
LANGUAGE plpgsql STABLE AS $$
DECLARE v_sub TEXT; v_tok TEXT;
BEGIN
    v_sub := current_setting('chronos.auth_sub',   true);
    v_tok := current_setting('chronos.auth_token', true);
    IF v_sub IS NULL OR v_sub = '' OR v_tok IS NULL OR v_tok = '' THEN
        RETURN FALSE;
    END IF;
    RETURN v_tok = encode(hmac(v_sub, 'CHRONOS_MASTER_SECRET_2026', 'sha256'), 'hex');
END $$;

-- ---------- Mechanism A: self-healing BEFORE trigger ----------------
CREATE OR REPLACE FUNCTION fn_trg_self_healing() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_true_points  INT;
    v_true_penalty INT;
BEGIN
    -- Legitimate path (inside sp_record_verdict): sign the row and let it through.
    IF fn_is_authorized() THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        NEW.integrity_signature := fn_compute_signature(NEW.contest_id, NEW.user_id,
                                                        NEW.total_points, NEW.total_penalty_sec);
        NEW.last_updated := clock_timestamp();
        RETURN NEW;
    END IF;

    -- ---- unauthorized from here on ----
    IF TG_OP = 'INSERT' THEN
        INSERT INTO intrusion_alerts(target_table, contest_id, user_id, attempted_points, restored_points, incident_details)
        VALUES ('leaderboard_scores', NEW.contest_id, NEW.user_id, NEW.total_points, 0,
                'Unauthorized INSERT silently discarded (no valid chronos.auth_token)');
        RETURN NULL;                       -- row is skipped, statement continues
    END IF;

    -- Truth = latest ledger state; fall back to the current row if no ledger entry exists.
    SELECT l.new_points, l.new_penalty_sec
      INTO v_true_points, v_true_penalty
      FROM score_audit_ledger l
     WHERE l.contest_id = OLD.contest_id AND l.user_id = OLD.user_id
     ORDER BY l.event_id DESC
     LIMIT 1;
    IF NOT FOUND THEN
        v_true_points  := OLD.total_points;
        v_true_penalty := OLD.total_penalty_sec;
    END IF;

    IF TG_OP = 'DELETE' THEN
        INSERT INTO intrusion_alerts(target_table, contest_id, user_id, attempted_points, restored_points, incident_details)
        VALUES ('leaderboard_scores', OLD.contest_id, OLD.user_id, 0, v_true_points,
                'Unauthorized DELETE cancelled; row preserved');
        RETURN NULL;                       -- cancels the delete
    END IF;

    -- UPDATE
    IF NEW.total_points        IS DISTINCT FROM v_true_points
    OR NEW.total_penalty_sec   IS DISTINCT FROM v_true_penalty
    OR NEW.contest_id          IS DISTINCT FROM OLD.contest_id
    OR NEW.user_id             IS DISTINCT FROM OLD.user_id
    OR NEW.integrity_signature IS DISTINCT FROM OLD.integrity_signature THEN
        INSERT INTO intrusion_alerts(target_table, contest_id, user_id, attempted_points, restored_points, incident_details)
        VALUES ('leaderboard_scores', OLD.contest_id, OLD.user_id, NEW.total_points, v_true_points,
                format('Unauthorized UPDATE healed: attempted points=%s penalty=%s; restored points=%s penalty=%s',
                       NEW.total_points, NEW.total_penalty_sec, v_true_points, v_true_penalty));
    END IF;

    NEW.contest_id          := OLD.contest_id;
    NEW.user_id             := OLD.user_id;
    NEW.total_points        := v_true_points;
    NEW.total_penalty_sec   := v_true_penalty;
    NEW.integrity_signature := fn_compute_signature(OLD.contest_id, OLD.user_id, v_true_points, v_true_penalty);
    NEW.last_updated        := OLD.last_updated;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_self_healing_leaderboard
    BEFORE INSERT OR UPDATE OR DELETE ON leaderboard_scores
    FOR EACH ROW EXECUTE FUNCTION fn_trg_self_healing();

-- ---------- Mechanism B: hash-chained ledger ------------------------
CREATE OR REPLACE FUNCTION fn_trg_append_ledger() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_prev    TEXT;
    v_now     TIMESTAMPTZ;
    v_sub     BIGINT;
    v_old_pts INT := 0;
    v_old_pen INT := 0;
BEGIN
    IF NOT fn_is_authorized() THEN RETURN NULL; END IF;     -- healed/blocked writes are never recorded as scores

    IF TG_OP = 'UPDATE' THEN
        IF NEW.total_points = OLD.total_points AND NEW.total_penalty_sec = OLD.total_penalty_sec THEN
            RETURN NULL;
        END IF;
        v_old_pts := OLD.total_points;
        v_old_pen := OLD.total_penalty_sec;
    END IF;

    PERFORM pg_advisory_xact_lock(727274);   -- serialize chain appends (prevents forked chains)

    SELECT current_event_hash INTO v_prev FROM score_audit_ledger ORDER BY event_id DESC LIMIT 1;
    v_prev := COALESCE(v_prev, fn_genesis_hash());
    v_now  := clock_timestamp();
    v_sub  := NULLIF(current_setting('chronos.auth_sub', true), '')::BIGINT;

    INSERT INTO score_audit_ledger(contest_id, user_id, submission_id, old_points, new_points,
                                   old_penalty_sec, new_penalty_sec, recorded_at,
                                   prev_event_hash, current_event_hash)
    VALUES (NEW.contest_id, NEW.user_id, v_sub, v_old_pts, NEW.total_points,
            v_old_pen, NEW.total_penalty_sec, v_now, v_prev,
            fn_compute_event_hash(v_prev, NEW.contest_id, NEW.user_id,
                                  NEW.total_points, NEW.total_penalty_sec, v_now));
    RETURN NULL;
END $$;

CREATE TRIGGER trg_append_ledger
    AFTER INSERT OR UPDATE ON leaderboard_scores
    FOR EACH ROW EXECUTE FUNCTION fn_trg_append_ledger();

-- Ledger is append-only
CREATE OR REPLACE FUNCTION fn_trg_ledger_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'score_audit_ledger is append-only: % is not allowed', TG_OP;
END $$;

CREATE TRIGGER trg_ledger_immutable_row
    BEFORE UPDATE OR DELETE ON score_audit_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_trg_ledger_immutable();

CREATE TRIGGER trg_ledger_immutable_truncate
    BEFORE TRUNCATE ON score_audit_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION fn_trg_ledger_immutable();

-- ---------- The ONLY legitimate way to change scores -----------------
CREATE OR REPLACE PROCEDURE sp_record_verdict(
    p_submission_id BIGINT, p_points_delta INT, p_penalty_delta INT)
LANGUAGE plpgsql AS $$
DECLARE
    v_contest INT;
    v_user    INT;
BEGIN
    IF p_points_delta < 0 OR p_penalty_delta < 0 THEN
        RAISE EXCEPTION 'deltas must be >= 0';
    END IF;

    SELECT p.contest_id, s.user_id INTO v_contest, v_user
      FROM submissions s JOIN problems p ON p.problem_id = s.problem_id
     WHERE s.submission_id = p_submission_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'submission % not found', p_submission_id;
    END IF;

    -- transaction-local credentials (is_local = true)
    PERFORM set_config('chronos.auth_sub',   p_submission_id::text, true);
    PERFORM set_config('chronos.auth_token',
                       encode(hmac(p_submission_id::text, 'CHRONOS_MASTER_SECRET_2026', 'sha256'), 'hex'), true);

    IF p_points_delta > 0 THEN
        UPDATE submissions SET status = 'ACCEPTED' WHERE submission_id = p_submission_id;
    END IF;

    PERFORM 1 FROM leaderboard_scores WHERE contest_id = v_contest AND user_id = v_user FOR UPDATE;
    IF FOUND THEN
        UPDATE leaderboard_scores
           SET total_points      = total_points + p_points_delta,
               total_penalty_sec = total_penalty_sec + p_penalty_delta
         WHERE contest_id = v_contest AND user_id = v_user;
    ELSE
        INSERT INTO leaderboard_scores(contest_id, user_id, total_points, total_penalty_sec, integrity_signature)
        VALUES (v_contest, v_user, p_points_delta, p_penalty_delta, repeat('0', 64));  -- trigger replaces signature
    END IF;

    -- wipe credentials immediately
    PERFORM set_config('chronos.auth_token', '', true);
    PERFORM set_config('chronos.auth_sub',   '', true);
END $$;

-- ---------- Chain verification (single query, window function) -------
CREATE OR REPLACE FUNCTION fn_verify_ledger_chain()
RETURNS TABLE (is_valid BOOLEAN, total_events BIGINT, broken_events BIGINT,
               first_broken_event_id BIGINT, message TEXT)
LANGUAGE sql STABLE AS $$
    WITH chk AS (
        SELECT l.event_id,
               (l.prev_event_hash <> COALESCE(LAG(l.current_event_hash) OVER (ORDER BY l.event_id),
                                              fn_genesis_hash())) AS link_broken,
               (l.current_event_hash <> fn_compute_event_hash(l.prev_event_hash, l.contest_id, l.user_id,
                                          l.new_points, l.new_penalty_sec, l.recorded_at)) AS hash_broken
        FROM score_audit_ledger l
    )
    SELECT (COUNT(*) FILTER (WHERE link_broken OR hash_broken) = 0),
           COUNT(*),
           COUNT(*) FILTER (WHERE link_broken OR hash_broken),
           MIN(event_id) FILTER (WHERE link_broken OR hash_broken),
           CASE WHEN COUNT(*) FILTER (WHERE link_broken OR hash_broken) = 0
                THEN 'Chain intact: every event hash and every prev-link verified'
                ELSE 'TAMPERING DETECTED: chain is broken' END
    FROM chk;
$$;
