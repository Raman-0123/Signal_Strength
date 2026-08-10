CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled, updated_at);
CREATE INDEX IF NOT EXISTS idx_evidence_expiry ON evidence_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_import_keys_lookup ON dedup_import_keys(kind, value);
