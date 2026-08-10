CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    qualified_count INTEGER NOT NULL DEFAULT 0,
    strict_count INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    runner_id TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS job_queries (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    query_index INTEGER NOT NULL,
    bucket TEXT NOT NULL,
    primary_query TEXT NOT NULL,
    fallback_query TEXT NOT NULL DEFAULT '',
    state_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, query_index)
);

CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    identity_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    designation TEXT NOT NULL,
    company TEXT NOT NULL,
    verified_location TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_identity ON leads(identity_key);

CREATE TABLE IF NOT EXISTS job_leads (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    strict_qualified INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 50,
    query_bucket TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    evaluation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, lead_id)
);

CREATE INDEX IF NOT EXISTS idx_job_leads_strict ON job_leads(job_id, strict_qualified);

CREATE TABLE IF NOT EXISTS dedup_keys (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    lead_id TEXT REFERENCES leads(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'lead',
    created_at TEXT NOT NULL,
    PRIMARY KEY(kind, value)
);

CREATE TABLE IF NOT EXISTS evidence_cache (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cache_kind TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    evidence_text TEXT NOT NULL DEFAULT '',
    result_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(job_id, cache_kind, cache_key)
);

CREATE TABLE IF NOT EXISTS dedup_imports (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sheet_count INTEGER NOT NULL DEFAULT 0,
    key_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dedup_import_keys (
    import_id TEXT NOT NULL REFERENCES dedup_imports(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(import_id, kind, value)
);

CREATE TABLE IF NOT EXISTS job_artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job_type ON job_artifacts(job_id, artifact_type);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workflow TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_job_id TEXT NOT NULL DEFAULT '',
    next_run_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job_id ON job_events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_events_created ON job_events(created_at);

CREATE TABLE IF NOT EXISTS job_metrics (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, name)
);

CREATE TABLE IF NOT EXISTS browser_sessions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

