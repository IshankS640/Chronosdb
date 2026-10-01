-- =====================================================================
-- ChronosDB  |  01_schema_bcnf.sql  |  11 tables, enums, constraints
-- Run ONCE on an empty database.  (To re-run: drop & recreate the database)
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE user_role         AS ENUM ('CONTESTANT', 'JUDGE_ADMIN', 'AUDITOR');
CREATE TYPE submission_status AS ENUM ('QUEUED', 'PROCESSING', 'ACCEPTED', 'WRONG_ANSWER', 'TIME_LIMIT_EXCEEDED', 'SYSTEM_ERROR');
CREATE TYPE worker_status     AS ENUM ('IDLE', 'BUSY', 'OFFLINE');

-- 1 ---------------------------------------------------------------
CREATE TABLE users (
    user_id     SERIAL       PRIMARY KEY,
    handle      VARCHAR(50)  UNIQUE NOT NULL,
    email       VARCHAR(100) UNIQUE NOT NULL,
    role        user_role    NOT NULL DEFAULT 'CONTESTANT',
    rating      INT          NOT NULL DEFAULT 1200 CHECK (rating >= 0),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 2 ---------------------------------------------------------------
CREATE TABLE contests (
    contest_id  SERIAL       PRIMARY KEY,
    title       VARCHAR(150) NOT NULL,
    start_time  TIMESTAMPTZ  NOT NULL,
    end_time    TIMESTAMPTZ  NOT NULL,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_contest_time CHECK (end_time > start_time)
);

-- 3 ---------------------------------------------------------------
CREATE TABLE problems (
    problem_id      SERIAL       PRIMARY KEY,
    contest_id      INT          NOT NULL REFERENCES contests(contest_id) ON DELETE CASCADE,
    code            VARCHAR(10)  NOT NULL,
    title           VARCHAR(150) NOT NULL,
    points          INT          NOT NULL CHECK (points > 0),
    time_limit_ms   INT          NOT NULL DEFAULT 2000 CHECK (time_limit_ms BETWEEN 100 AND 10000),
    memory_limit_kb INT          NOT NULL DEFAULT 262144,
    UNIQUE (contest_id, code)
);

-- 4 ---------------------------------------------------------------
CREATE TABLE test_cases (
    testcase_id          SERIAL   PRIMARY KEY,
    problem_id           INT      NOT NULL REFERENCES problems(problem_id) ON DELETE CASCADE,
    input_hash           CHAR(64) NOT NULL,
    expected_output_hash CHAR(64) NOT NULL,
    is_sample            BOOLEAN  NOT NULL DEFAULT FALSE
);

-- 5 ---------------------------------------------------------------
CREATE TABLE contest_registrations (
    contest_id    INT         NOT NULL REFERENCES contests(contest_id) ON DELETE CASCADE,
    user_id       INT         NOT NULL REFERENCES users(user_id)       ON DELETE CASCADE,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contest_id, user_id)
);

-- 6 ---------------------------------------------------------------
CREATE TABLE judge_workers (
    worker_id       SERIAL      PRIMARY KEY,
    worker_name     VARCHAR(50) UNIQUE NOT NULL,
    status          worker_status NOT NULL DEFAULT 'IDLE',
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_count INT         NOT NULL DEFAULT 0
);

-- 7 ---------------------------------------------------------------
-- NOTE: contest_id is intentionally NOT stored here. In 'problems',
-- problem_id -> contest_id, so repeating contest_id in submissions would be a
-- redundant (transitive) dependency, violating 3NF/BCNF. Reach it via problems.
CREATE TABLE submissions (
    submission_id    BIGSERIAL         PRIMARY KEY,
    problem_id       INT               NOT NULL REFERENCES problems(problem_id),
    user_id          INT               NOT NULL REFERENCES users(user_id),
    language         VARCHAR(20)       NOT NULL CHECK (language IN ('CPP17','PYTHON3','RUST','JAVA')),
    source_code_hash CHAR(64)          NOT NULL,
    code_metadata    JSONB             NOT NULL DEFAULT '{}'::jsonb,
    status           submission_status NOT NULL DEFAULT 'QUEUED',
    execution_time_ms INT              DEFAULT NULL,
    submitted_at     TIMESTAMPTZ       NOT NULL DEFAULT NOW()
);

-- 8 ---------------------------------------------------------------
CREATE TABLE submission_queue (
    queue_id            BIGSERIAL PRIMARY KEY,
    submission_id       BIGINT    UNIQUE NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
    priority            INT       NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 10),
    locked_by_worker_id INT       REFERENCES judge_workers(worker_id) ON DELETE SET NULL,
    locked_at           TIMESTAMPTZ DEFAULT NULL,
    attempt_count       INT       NOT NULL DEFAULT 0 CHECK (attempt_count <= 5)
);

-- 9 ---------------------------------------------------------------
CREATE TABLE leaderboard_scores (
    contest_id          INT         NOT NULL REFERENCES contests(contest_id),
    user_id             INT         NOT NULL REFERENCES users(user_id),
    total_points        INT         NOT NULL DEFAULT 0 CHECK (total_points >= 0),
    total_penalty_sec   INT         NOT NULL DEFAULT 0 CHECK (total_penalty_sec >= 0),
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    integrity_signature CHAR(64)    NOT NULL,
    PRIMARY KEY (contest_id, user_id)
);

-- 10 --------------------------------------------------------------
CREATE TABLE score_audit_ledger (
    event_id           BIGSERIAL   PRIMARY KEY,
    contest_id         INT         NOT NULL,
    user_id            INT         NOT NULL,
    submission_id      BIGINT      REFERENCES submissions(submission_id),
    old_points         INT         NOT NULL,
    new_points         INT         NOT NULL,
    old_penalty_sec    INT         NOT NULL,
    new_penalty_sec    INT         NOT NULL,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    prev_event_hash    CHAR(64)    NOT NULL,
    current_event_hash CHAR(64)    NOT NULL
);

-- 11 --------------------------------------------------------------
CREATE TABLE intrusion_alerts (
    alert_id         SERIAL       PRIMARY KEY,
    target_table     VARCHAR(50)  NOT NULL,
    contest_id       INT          NOT NULL,
    user_id          INT          NOT NULL,
    attempted_points INT          NOT NULL,
    restored_points  INT          NOT NULL,
    db_user          VARCHAR(50)  NOT NULL DEFAULT current_user,
    client_addr      TEXT         DEFAULT inet_client_addr()::TEXT,
    detected_at      TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    incident_details TEXT         NOT NULL
);
