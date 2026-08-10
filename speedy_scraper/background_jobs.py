from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

JOBS_ROOT = Path("exports/jobs")


def create_job(workflow: str, config: dict[str, object], *, jobs_root: Path = JOBS_ROOT) -> Path:
    job_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    job_dir = jobs_root / workflow / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    write_json(job_dir / "config.json", config)
    write_status(
        job_dir,
        state="queued",
        workflow=workflow,
        job_id=job_id,
        message="Ready to start",
        processed=0,
        total=0,
    )
    return job_dir


def launch_job(job_dir: Path | str, module: str) -> int:
    path = Path(job_dir).resolve()
    status = read_status(path)
    pid = int(status.get("pid") or 0)
    if status.get("state") in {"running", "stopping"} and process_is_running(pid):
        return pid

    stop_path = path / "stop.requested"
    if stop_path.exists():
        stop_path.unlink()
    status.update(state="starting", message="Launching background worker")
    write_status(path, **status)
    log_handle = (path / "worker.log").open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", module, "--job-dir", str(path)],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    latest = read_status(path)
    latest["pid"] = process.pid
    if latest.get("state") == "starting":
        latest.update(state="running", message="Background worker started")
    write_status(path, **latest)
    return process.pid


def request_stop(job_dir: Path | str) -> None:
    path = Path(job_dir)
    (path / "stop.requested").touch()
    status = read_status(path)
    status.update(
        state="stopping",
        message="Stop requested; saving the current item before pausing",
    )
    write_status(path, **status)


def stop_requested(job_dir: Path | str) -> bool:
    return (Path(job_dir) / "stop.requested").exists()


def clear_stop(job_dir: Path | str) -> None:
    path = Path(job_dir) / "stop.requested"
    if path.exists():
        path.unlink()


def read_status(job_dir: Path | str) -> dict[str, Any]:
    return read_json(Path(job_dir) / "status.json", default={})


def write_status(job_dir: Path | str, **values: object) -> None:
    path = Path(job_dir)
    values["updated_at"] = datetime.now(UTC).isoformat()
    write_json(path / "status.json", values)


def update_status(job_dir: Path | str, **updates: object) -> dict[str, Any]:
    current = read_status(job_dir)
    current.update(updates)
    write_status(job_dir, **current)
    return current


class JobHeartbeat:
    def __init__(self, job_dir: Path | str, **activity: object):
        self.job_dir = Path(job_dir)
        self.activity = activity
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pulse, daemon=True)

    def __enter__(self) -> JobHeartbeat:
        self._write(0)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _pulse(self) -> None:
        tick = 0
        while not self._stop.wait(2):
            tick += 1
            if stop_requested(self.job_dir):
                return
            self._write(tick)

    def _write(self, tick: int) -> None:
        update_status(
            self.job_dir,
            heartbeat_at=datetime.now(UTC).isoformat(),
            heartbeat_tick=tick,
            **self.activity,
        )


def read_json(path: Path | str, *, default: Any = None) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path | str, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def list_jobs(workflow: str, *, jobs_root: Path = JOBS_ROOT) -> list[Path]:
    directory = jobs_root / workflow
    if not directory.exists():
        return []
    return sorted((path for path in directory.iterdir() if path.is_dir()), reverse=True)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def heartbeat_age_seconds(status: dict[str, Any]) -> int | None:
    raw_value = status.get("heartbeat_at")
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


def job_is_stale(status: dict[str, Any], *, stale_after_seconds: int = 30) -> bool:
    if status.get("state") not in {"starting", "running", "stopping"}:
        return False
    pid = int(status.get("pid") or 0)
    if process_is_running(pid):
        return False
    age = heartbeat_age_seconds(status)
    if age is None:
        age = _timestamp_age_seconds(status.get("updated_at"))
    return age is None or age > stale_after_seconds


def _timestamp_age_seconds(raw_value: object) -> int | None:
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))
