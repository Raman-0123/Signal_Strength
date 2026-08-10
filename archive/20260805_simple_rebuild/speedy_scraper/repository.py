"""SQLite repositories and durable job state transitions."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Iterator
from uuid import uuid4

from core.utils import normalize_linkedin_url, person_identity_key
from speedy_scraper.domain import (
    CandidateEvaluation,
    DedupKey,
    DedupScope,
    JobRecord,
    JobStatus,
    LeadRecord,
    ResultKind,
    ScheduleRecord,
    StorageConfig,
    utc_now,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"
if not MIGRATIONS_DIR.exists():
    MIGRATIONS_DIR = Path(sys.prefix) / "migrations"


_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.COMPLETED},
    JobStatus.RUNNING: {
        JobStatus.WAITING_VERIFICATION, JobStatus.PAUSE_REQUESTED,
        JobStatus.CANCEL_REQUESTED, JobStatus.COMPLETED,
        JobStatus.EXHAUSTED, JobStatus.FAILED,
    },
    JobStatus.WAITING_VERIFICATION: {
        JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.CANCELLED, JobStatus.FAILED,
    },
    JobStatus.PAUSE_REQUESTED: {JobStatus.PAUSED, JobStatus.FAILED},
    JobStatus.CANCEL_REQUESTED: {JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.PAUSED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.FAILED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.EXHAUSTED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.CANCELLED: set(),
    JobStatus.COMPLETED: set(),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | bytes | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class LeadRepository:
    """SQLite is the source of truth for jobs, results, and deduplication."""

    def __init__(self, storage: StorageConfig | str | Path):
        if isinstance(storage, StorageConfig):
            self.path = Path(storage.database_path).expanduser().resolve()
            self.busy_timeout_ms = storage.busy_timeout_ms
            self.retention_days = storage.retention_days
        else:
            self.path = Path(storage).expanduser().resolve()
            self.busy_timeout_ms = 10_000
            self.retention_days = 90
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._schema_version: int | None = None

    class _ClosingConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=max(1.0, self.busy_timeout_ms / 1000),
            isolation_level=None,
            check_same_thread=False,
            factory=self._ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def migrate(self) -> int:
        if self._schema_version is not None:
            return self._schema_version
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            latest = max(applied, default=0)
            for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                connection.executescript(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                    (version, path.name, utc_now()),
                )
                latest = max(latest, version)
        self._schema_version = latest
        return latest

    def create_job(
        self,
        workflow: str,
        request: dict[str, Any],
        *,
        checkpoint: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        self.migrate()
        if idempotency_key:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
            if row:
                return self._job_from_row(row)
        job_id = str(uuid4())
        now = utc_now()
        try:
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO jobs(
                        id, workflow, status, request_json, checkpoint_json,
                        idempotency_key, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        job_id, workflow, JobStatus.QUEUED.value, _json(request),
                        _json(checkpoint or {}), idempotency_key, now, now,
                    ),
                )
                for index, item in enumerate(request.get("query_plan", [])):
                    connection.execute(
                        """INSERT INTO job_queries(
                            job_id, query_index, bucket, primary_query,
                            fallback_query, updated_at
                        ) VALUES(?,?,?,?,?,?)""",
                        (
                            job_id, index, item.get("bucket", "discovery"),
                            item.get("query", ""), item.get("fallback_query", ""), now,
                        ),
                    )
        except sqlite3.IntegrityError:
            if not idempotency_key:
                raise
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
            if not row:
                raise
            return self._job_from_row(row)
        return self.get_job(job_id)

    def _job_from_row(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"], workflow=row["workflow"], status=JobStatus(row["status"]),
            request=_loads(row["request_json"], {}),
            checkpoint=_loads(row["checkpoint_json"], {}),
            qualified_count=int(row["qualified_count"]),
            strict_count=int(row["strict_count"]), outcome=row["outcome"],
            error_code=row["error_code"], error_message=row["error_message"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            started_at=row["started_at"], completed_at=row["completed_at"],
        )

    def get_job(self, job_id: str) -> JobRecord:
        self.migrate()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown job: {job_id}")
        return self._job_from_row(row)

    def get_job_by_idempotency_key(self, key: str) -> JobRecord | None:
        if not key:
            return None
        self.migrate()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def list_jobs(self, *, limit: int = 100, status: JobStatus | None = None) -> list[JobRecord]:
        self.migrate()
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._job_from_row(row) for row in rows]

    def transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        outcome: str | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> JobRecord:
        now = utc_now()
        completed = now if status.terminal else ""
        with self.transaction(immediate=True) as connection:
            current = connection.execute("SELECT status, started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not current:
                raise KeyError(f"Unknown job: {job_id}")
            current_status = JobStatus(current["status"])
            if status != current_status and status not in _ALLOWED_TRANSITIONS[current_status]:
                raise ValueError(
                    f"Invalid job transition: {current_status.value} -> {status.value}"
                )
            started_at = current["started_at"] or (now if status == JobStatus.RUNNING else "")
            connection.execute(
                """UPDATE jobs SET status=?, outcome=COALESCE(?, outcome),
                    error_code=?, error_message=?, updated_at=?, started_at=?,
                    completed_at=?, runner_id=CASE WHEN ? THEN '' ELSE runner_id END,
                    lease_expires_at=CASE WHEN ? THEN '' ELSE lease_expires_at END
                    WHERE id=?""",
                (
                    status.value, outcome, error_code, error_message, now,
                    started_at, completed, int(status != JobStatus.RUNNING),
                    int(status != JobStatus.RUNNING), job_id,
                ),
            )
        return self.get_job(job_id)

    def claim_next_job(
        self,
        runner_id: str,
        *,
        lease_seconds: int = 60,
        lane: str = "any",
    ) -> JobRecord | None:
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        google_expression = """(
            workflow='lead' AND (
                json_extract(request_json, '$.discovery_provider')='google'
                OR json_extract(request_json, '$.validation_provider')='google'
            )
        )"""
        lane_clause = ""
        if lane == "google":
            lane_clause = f" AND {google_expression}"
        elif lane == "non_google":
            lane_clause = f" AND NOT {google_expression}"
        elif lane != "any":
            raise ValueError(f"Unknown runner lane: {lane}")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                f"SELECT id FROM jobs WHERE status=? {lane_clause} ORDER BY created_at LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if not row:
                return None
            changed = connection.execute(
                """UPDATE jobs SET status=?, runner_id=?, lease_expires_at=?,
                    started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
                    updated_at=? WHERE id=? AND status=?""",
                (
                    JobStatus.RUNNING.value, runner_id, expiry, now.isoformat(),
                    now.isoformat(), row["id"], JobStatus.QUEUED.value,
                ),
            ).rowcount
            if not changed:
                return None
        return self.get_job(row["id"])

    def heartbeat(self, job_id: str, runner_id: str, *, lease_seconds: int = 60) -> bool:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? AND runner_id=? AND status=?",
                (expiry, utc_now(), job_id, runner_id, JobStatus.RUNNING.value),
            ).rowcount
        return bool(changed)

    def recover_stale_jobs(self) -> int:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status=? AND lease_expires_at<>'' AND lease_expires_at<?",
                (JobStatus.RUNNING.value, now),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET status=?, runner_id='', lease_expires_at='', updated_at=? WHERE id=?",
                    (JobStatus.QUEUED.value, now, row["id"]),
                )
        for row in rows:
            self.add_event(row["id"], "warning", "job_recovered", "Recovered an expired runner lease.")
        return len(rows)

    def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
                (_json(checkpoint), now, job_id),
            )
            query_index = int(checkpoint.get("query_index", 0))
            query_state = checkpoint.get("query_state", {})
            connection.execute(
                "UPDATE job_queries SET state_json=?, status=?, updated_at=? WHERE job_id=? AND query_index=?",
                (
                    _json(query_state),
                    "complete" if query_state.get("complete") else "running",
                    now, job_id, query_index,
                ),
            )
            connection.execute(
                """INSERT INTO browser_sessions(job_id, state_json, updated_at)
                   VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                   state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (job_id, _json(checkpoint.get("browser_state", {})), now),
            )

    def save_qualified(
        self,
        job_id: str,
        lead: LeadRecord,
        *,
        checkpoint: dict[str, Any] | None = None,
        pending_validation: dict[str, Any] | None = None,
    ) -> str:
        now = utc_now()
        canonical = normalize_linkedin_url(lead.linkedin_url)
        if not canonical:
            raise ValueError("Lead requires a canonical LinkedIn URL")
        identity = person_identity_key(lead.name, lead.company)
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM leads WHERE canonical_url=?", (canonical,)
            ).fetchone()
            lead_id = existing["id"] if existing else str(uuid4())
            if existing:
                connection.execute(
                    """UPDATE leads SET identity_key=?, name=?, designation=?, company=?,
                        verified_location=?, last_seen_at=? WHERE id=?""",
                    (
                        identity, lead.name, lead.designation, lead.company,
                        lead.verified_location, now, lead_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO leads(id, canonical_url, identity_key, name,
                        designation, company, verified_location, first_seen_at, last_seen_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        lead_id, canonical, identity, lead.name, lead.designation,
                        lead.company, lead.verified_location, now, now,
                    ),
                )
            connection.execute(
                """INSERT INTO job_leads(job_id, lead_id, strict_qualified, score,
                    query_bucket, source, payload_json, evaluation_json, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id, lead_id) DO UPDATE SET
                    score=excluded.score, query_bucket=excluded.query_bucket,
                    source=excluded.source, payload_json=excluded.payload_json,
                    evaluation_json=excluded.evaluation_json, updated_at=excluded.updated_at""",
                (
                    job_id, lead_id, int(lead.strict_qualified), lead.score,
                    lead.query_bucket, lead.source, _json(lead.as_dict()),
                    _json(lead.evaluation), now, now,
                ),
            )
            for kind, key in (("url", canonical), ("identity", identity)):
                if key:
                    connection.execute(
                        "INSERT OR IGNORE INTO dedup_keys(kind,value,lead_id,source,created_at) VALUES(?,?,?,?,?)",
                        (kind, key, lead_id, "lead", now),
                    )
            counts = connection.execute(
                """SELECT COUNT(*) AS qualified,
                    SUM(CASE WHEN strict_qualified=1 THEN 1 ELSE 0 END) AS strict
                    FROM job_leads WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            connection.execute(
                "UPDATE jobs SET qualified_count=?, strict_count=?, updated_at=? WHERE id=?",
                (counts["qualified"], counts["strict"] or 0, now, job_id),
            )
            if checkpoint is not None:
                persisted_checkpoint = json.loads(_json(checkpoint))
                if pending_validation is not None:
                    persisted_checkpoint["pending_validation"] = {
                        **pending_validation,
                        "lead_id": lead_id,
                    }
                connection.execute(
                    "UPDATE jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
                    (_json(persisted_checkpoint), now, job_id),
                )
                query_index = int(persisted_checkpoint.get("query_index", 0))
                query_state = persisted_checkpoint.get("query_state", {})
                connection.execute(
                    """UPDATE job_queries SET state_json=?, status=?, updated_at=?
                       WHERE job_id=? AND query_index=?""",
                    (
                        _json(query_state),
                        "complete" if query_state.get("complete") else "running",
                        now, job_id, query_index,
                    ),
                )
                connection.execute(
                    """INSERT INTO browser_sessions(job_id, state_json, updated_at)
                       VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       state_json=excluded.state_json, updated_at=excluded.updated_at""",
                    (job_id, _json(persisted_checkpoint.get("browser_state", {})), now),
                )
        return lead_id

    def update_strict_result(
        self,
        job_id: str,
        lead_id: str,
        evaluation: CandidateEvaluation,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            if payload is None:
                connection.execute(
                    "UPDATE job_leads SET strict_qualified=?, evaluation_json=?, updated_at=? WHERE job_id=? AND lead_id=?",
                    (int(evaluation.strict_qualified), _json(evaluation.as_dict()), now, job_id, lead_id),
                )
            else:
                connection.execute(
                    "UPDATE job_leads SET strict_qualified=?, evaluation_json=?, payload_json=?, updated_at=? WHERE job_id=? AND lead_id=?",
                    (
                        int(evaluation.strict_qualified), _json(evaluation.as_dict()),
                        _json(payload), now, job_id, lead_id,
                    ),
                )
            strict_count = connection.execute(
                "SELECT COUNT(*) FROM job_leads WHERE job_id=? AND strict_qualified=1",
                (job_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE jobs SET strict_count=?, updated_at=? WHERE id=?",
                (strict_count, now, job_id),
            )

    def contains_dedup_key(
        self,
        key: DedupKey | str,
        value: str | DedupScope | None = None,
        *,
        scope: DedupScope = DedupScope.GLOBAL,
        job_id: str = "",
        import_ids: Iterable[str] = (),
        exclude_job_id: str = "",
    ) -> bool:
        if isinstance(key, DedupKey):
            kind, normalized_value = key.kind, key.value
            if isinstance(value, DedupScope):
                scope = value
        else:
            kind, normalized_value = key, str(value or "")
        value = normalized_value
        if kind == "url":
            value = normalize_linkedin_url(value)
        if not value:
            return False
        self.migrate()
        with self.connect() as connection:
            if scope != DedupScope.IMPORT:
                row = connection.execute(
                    "SELECT lead_id FROM dedup_keys WHERE kind=? AND value=?", (kind, value)
                ).fetchone()
                if row and scope == DedupScope.JOB:
                    if not job_id:
                        raise ValueError("job_id is required for job-scoped deduplication")
                    return bool(connection.execute(
                        "SELECT 1 FROM job_leads WHERE job_id=? AND lead_id=?",
                        (job_id, row["lead_id"]),
                    ).fetchone())
                if row:
                    if not exclude_job_id:
                        return True
                    linked = connection.execute(
                        "SELECT 1 FROM job_leads WHERE job_id=? AND lead_id=?",
                        (exclude_job_id, row["lead_id"]),
                    ).fetchone()
                    if not linked:
                        return True
            ids = list(import_ids)
            if ids and scope != DedupScope.JOB:
                placeholders = ",".join("?" for _ in ids)
                imported = connection.execute(
                    f"SELECT 1 FROM dedup_import_keys WHERE kind=? AND value=? AND import_id IN ({placeholders}) LIMIT 1",
                    [kind, value, *ids],
                ).fetchone()
                if imported:
                    return True
        return False

    def list_results(self, job_id: str, kind: ResultKind = ResultKind.QUALIFIED) -> list[LeadRecord]:
        clause = "AND jl.strict_qualified=1" if kind == ResultKind.STRICT else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT l.*, jl.score, jl.query_bucket, jl.source,
                    jl.strict_qualified, jl.payload_json, jl.evaluation_json
                    FROM job_leads jl JOIN leads l ON l.id=jl.lead_id
                    WHERE jl.job_id=? {clause}
                    ORDER BY jl.created_at, l.name""",
                (job_id,),
            ).fetchall()
        records = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            records.append(LeadRecord(
                name=row["name"], designation=row["designation"],
                company=row["company"], linkedin_url=row["canonical_url"],
                verified_location=row["verified_location"], score=int(row["score"]),
                query_bucket=row["query_bucket"], source=row["source"],
                hard_qualified=True, strict_qualified=bool(row["strict_qualified"]),
                evaluation=_loads(row["evaluation_json"], payload.get("evaluation", {})),
                created_at=payload.get("created_at", row["first_seen_at"]),
            ))
        return records

    def get_evidence(self, job_id: str, cache_kind: str, cache_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM evidence_cache
                   WHERE job_id=? AND cache_kind=? AND cache_key=?
                   AND (expires_at='' OR expires_at>?)""",
                (job_id, cache_kind, cache_key, utc_now()),
            ).fetchone()
        return dict(row) if row else None

    def save_evidence(
        self,
        job_id: str,
        cache_kind: str,
        cache_key: str,
        provider: str,
        query: str,
        evidence_text: str,
        result_count: int,
    ) -> None:
        now = utc_now()
        expires = (
            datetime.now(timezone.utc) + timedelta(days=self.retention_days)
        ).isoformat()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO evidence_cache(job_id,cache_kind,cache_key,provider,
                    query,evidence_text,result_count,created_at,expires_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id,cache_kind,cache_key) DO UPDATE SET
                    provider=excluded.provider, query=excluded.query,
                    evidence_text=excluded.evidence_text,
                    result_count=excluded.result_count, created_at=excluded.created_at,
                    expires_at=excluded.expires_at""",
                (
                    job_id, cache_kind, cache_key, provider, query,
                    evidence_text, result_count, now, expires,
                ),
            )

    def add_event(
        self,
        job_id: str,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO job_events(job_id,level,event_type,message,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (job_id, level, event_type, message, _json(payload or {}), utc_now()),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event identifier.")
        return int(cursor.lastrowid)

    def list_events(self, job_id: str, *, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, max(0, int(after_id)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return [{**dict(row), "payload": _loads(row["payload_json"], {})} for row in rows]

    def increment_metric(self, job_id: str, name: str, amount: float = 1.0) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO job_metrics(job_id,name,value,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(job_id,name) DO UPDATE SET
                    value=job_metrics.value+excluded.value, updated_at=excluded.updated_at""",
                (job_id, name, float(amount), utc_now()),
            )

    def set_metric(self, job_id: str, name: str, value: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO job_metrics(job_id,name,value,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(job_id,name) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""",
                (job_id, name, float(value), utc_now()),
            )

    def metrics(self, job_id: str | None = None) -> dict[str, float]:
        with self.connect() as connection:
            if job_id:
                rows = connection.execute(
                    "SELECT name,value FROM job_metrics WHERE job_id=?", (job_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT name,SUM(value) AS value FROM job_metrics GROUP BY name"
                ).fetchall()
        return {row["name"]: float(row["value"]) for row in rows}

    def save_artifact(self, job_id: str, artifact_type: str, payload: Any) -> str:
        artifact_id = str(uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO job_artifacts(id,job_id,artifact_type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (artifact_id, job_id, artifact_type, _json(payload), utc_now()),
            )
        return artifact_id

    def replace_artifact(self, job_id: str, artifact_type: str, payload: Any) -> str:
        """Atomically keep exactly one current artifact of the requested type."""

        artifact_id = str(uuid4())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM job_artifacts WHERE job_id=? AND artifact_type=?",
                (job_id, artifact_type),
            )
            connection.execute(
                "INSERT INTO job_artifacts(id,job_id,artifact_type,payload_json,created_at) VALUES(?,?,?,?,?)",
                (artifact_id, job_id, artifact_type, _json(payload), utc_now()),
            )
        return artifact_id

    def list_artifacts(self, job_id: str, artifact_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM job_artifacts WHERE job_id=?"
        params: list[Any] = [job_id]
        if artifact_type:
            sql += " AND artifact_type=?"
            params.append(artifact_type)
        sql += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [{**dict(row), "payload": _loads(row["payload_json"], {})} for row in rows]

    def create_dedup_import(self, name: str, keys: Iterable[tuple[str, str]], sheet_count: int = 1) -> str:
        import_id = str(uuid4())
        unique = {(kind, value) for kind, value in keys if kind and value}
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO dedup_imports(id,name,sheet_count,key_count,created_at) VALUES(?,?,?,?,?)",
                (import_id, name, max(1, int(sheet_count)), len(unique), utc_now()),
            )
            connection.executemany(
                "INSERT INTO dedup_import_keys(import_id,kind,value) VALUES(?,?,?)",
                [(import_id, kind, value) for kind, value in unique],
            )
        return import_id

    def list_dedup_imports(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,name,sheet_count,key_count,created_at FROM dedup_imports ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_dedup_import(self, import_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            changed = connection.execute(
                "DELETE FROM dedup_imports WHERE id=?", (import_id,),
            ).rowcount
        return bool(changed)

    def upsert_schedule(self, schedule: ScheduleRecord) -> ScheduleRecord:
        now = utc_now()
        created = schedule.created_at or now
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO schedules(id,name,workflow,trigger_json,request_json,timezone,
                    enabled,last_job_id,next_run_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    workflow=excluded.workflow,trigger_json=excluded.trigger_json,
                    request_json=excluded.request_json,timezone=excluded.timezone,
                    enabled=excluded.enabled,last_job_id=excluded.last_job_id,
                    next_run_at=excluded.next_run_at,updated_at=excluded.updated_at""",
                (
                    schedule.id, schedule.name, schedule.workflow, _json(schedule.trigger),
                    _json(schedule.request), schedule.timezone, int(schedule.enabled),
                    schedule.last_job_id, schedule.next_run_at, created, now,
                ),
            )
        return self.get_schedule(schedule.id)

    def _schedule_from_row(self, row: sqlite3.Row) -> ScheduleRecord:
        return ScheduleRecord(
            id=row["id"], name=row["name"], workflow=row["workflow"],
            trigger=_loads(row["trigger_json"], {}), request=_loads(row["request_json"], {}),
            timezone=row["timezone"], enabled=bool(row["enabled"]),
            last_job_id=row["last_job_id"], next_run_at=row["next_run_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get_schedule(self, schedule_id: str) -> ScheduleRecord:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown schedule: {schedule_id}")
        return self._schedule_from_row(row)

    def list_schedules(self, *, enabled_only: bool = False) -> list[ScheduleRecord]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(sql).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            changed = connection.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount
        return bool(changed)

    def cleanup(self) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        counts: dict[str, int] = {}
        with self.transaction(immediate=True) as connection:
            counts["events"] = connection.execute(
                "DELETE FROM job_events WHERE created_at<?", (cutoff,)
            ).rowcount
            counts["metrics"] = connection.execute(
                """DELETE FROM job_metrics WHERE job_id IN (
                    SELECT id FROM jobs WHERE completed_at<>'' AND completed_at<?
                )""",
                (cutoff,),
            ).rowcount
            counts["evidence"] = connection.execute(
                """DELETE FROM evidence_cache
                   WHERE (expires_at<>'' AND expires_at<?)
                   OR job_id IN (
                       SELECT id FROM jobs WHERE completed_at<>'' AND completed_at<?
                   )""",
                (utc_now(), cutoff),
            ).rowcount
            counts["browser_sessions"] = connection.execute(
                """DELETE FROM browser_sessions WHERE job_id IN (
                    SELECT id FROM jobs WHERE completed_at<>'' AND completed_at<?
                )""",
                (cutoff,),
            ).rowcount
            counts["checkpoints"] = connection.execute(
                """UPDATE jobs SET checkpoint_json='{}'
                   WHERE completed_at<>'' AND completed_at<? AND checkpoint_json<>'{}'""",
                (cutoff,),
            ).rowcount
            connection.execute(
                """UPDATE job_queries SET state_json='{}'
                   WHERE job_id IN (
                       SELECT id FROM jobs WHERE completed_at<>'' AND completed_at<?
                   )""",
                (cutoff,),
            )
        return counts
