CREATE TABLE IF NOT EXISTS analytics_dim_class (
    class_id TEXT PRIMARY KEY,
    class_name TEXT NOT NULL,
    grade TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS analytics_dim_student (
    student_id TEXT PRIMARY KEY,
    student_name TEXT NOT NULL,
    class_id TEXT NOT NULL REFERENCES analytics_dim_class(class_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analytics_dim_student_class
    ON analytics_dim_student(class_id);

CREATE TABLE IF NOT EXISTS analytics_dim_article (
    article_id TEXT PRIMARY KEY,
    task_id TEXT,
    article_hash TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analytics_dim_article_task_id
    ON analytics_dim_article(task_id);

CREATE TABLE IF NOT EXISTS analytics_fact_attempt (
    submission_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES analytics_dim_student(student_id),
    class_id TEXT NOT NULL REFERENCES analytics_dim_class(class_id),
    article_id TEXT NOT NULL REFERENCES analytics_dim_article(article_id),
    submitted_at TIMESTAMPTZ NOT NULL,
    overall_score DOUBLE PRECISION,
    pron_score DOUBLE PRECISION,
    fluency_score DOUBLE PRECISION,
    intonation_score DOUBLE PRECISION,
    completeness_score DOUBLE PRECISION,
    missing_word_count INTEGER NOT NULL DEFAULT 0,
    weak_phoneme_count INTEGER NOT NULL DEFAULT 0,
    source_json_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analytics_fact_attempt_student_time
    ON analytics_fact_attempt(student_id, submitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_analytics_fact_attempt_class_time
    ON analytics_fact_attempt(class_id, submitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_analytics_fact_attempt_article_time
    ON analytics_fact_attempt(article_id, submitted_at DESC);

CREATE TABLE IF NOT EXISTS analytics_file_ingest_state (
    file_path TEXT PRIMARY KEY,
    file_size BIGINT NOT NULL,
    mtime_ns BIGINT NOT NULL,
    last_status TEXT NOT NULL,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS analytics_job_run (
    run_id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    processed_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analytics_job_run_started_at
    ON analytics_job_run(started_at DESC);
