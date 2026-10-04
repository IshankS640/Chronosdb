CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TYPE user_role AS ENUM ('CONTESTANT', 'JUDGE_ADMIN', 'AUDITOR');
CREATE TYPE submission_status AS ENUM ('QUEUED', 'RUNNING', 'ACCEPTED', 'WRONG_ANSWER', 'TIME_LIMIT_EXCEEDED', 'MEMORY_LIMIT_EXCEEDED', 'COMPILATION_ERROR', 'SYSTEM_ERROR');
CREATE TYPE worker_status AS ENUM ('IDLE', 'BUSY', 'OFFLINE');

CREATE TABLE users (
    user_id     BIGSERIAL PRIMARY KEY,
    handle      VARCHAR(40) NOT NULL UNIQUE,
    email       VARCHAR(255) NOT NULL UNIQUE,
    role        user_role NOT NULL DEFAULT 'CONTESTANT',
    rating      INT NOT NULL DEFAULT 1500 CHECK (rating >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE contests (
    contest_id  BIGSERIAL PRIMARY KEY,
    title       VARCHAR(120) NOT NULL,
    start_time  TIMESTAMPTZ NOT NULL,
    end_time    TIMESTAMPTZ NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_contest_duration CHECK (end_time > start_time)
);
