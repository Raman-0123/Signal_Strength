"""Command-line interface for local and API-backed operation."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from speedy_scraper.config import load_config
from speedy_scraper.domain import JobStatus
from speedy_scraper.gateway import ApiGateway, LocalGateway
from speedy_scraper.scheduler import SchedulerService

EXIT_INVALID = 2
EXIT_FAILED = 3
EXIT_VERIFICATION = 4


def _load_mapping(path: str) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return value


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _gateway(args, *, start_runner=False):
    if args.api_url:
        return ApiGateway(args.api_url, os.environ.get("SPEEDY_SCRAPER_API_KEY", ""))
    return LocalGateway(args.settings, start_runner=start_runner)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speedy-scraper", description="Persistent lead intelligence scraper")
    parser.add_argument("--settings", help="Optional application YAML configuration.")
    parser.add_argument("--api-url", default="", help="Use a running Speedy-Scraper API instead of local SQLite.")
    commands = parser.add_subparsers(dest="command", required=True)

    scrape = commands.add_parser("scrape", help="Create and execute a scraping job.")
    scrape_sub = scrape.add_subparsers(dest="action", required=True)
    run = scrape_sub.add_parser("run")
    run.add_argument("--job-config", required=True, help="Workflow request YAML.")
    run.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)

    jobs = commands.add_parser("jobs")
    jobs_sub = jobs.add_subparsers(dest="action", required=True)
    list_cmd = jobs_sub.add_parser("list")
    list_cmd.add_argument("--limit", type=int, default=100)
    for action in ("show", "pause", "resume", "cancel", "retry"):
        command = jobs_sub.add_parser(action)
        command.add_argument("job_id")

    export = commands.add_parser("export")
    export.add_argument("job_id")
    export.add_argument("--format", choices=("csv", "xlsx", "json"), default="xlsx")
    export.add_argument("--kind", choices=("qualified", "strict"), default="qualified")
    export.add_argument("--output")

    schedule = commands.add_parser("schedule")
    schedule_sub = schedule.add_subparsers(dest="action", required=True)
    add = schedule_sub.add_parser("add")
    add.add_argument("--schedule-config", required=True)
    schedule_sub.add_parser("list")
    for action in ("enable", "disable", "delete", "run-now"):
        command = schedule_sub.add_parser(action)
        command.add_argument("schedule_id")

    verification = commands.add_parser("verification")
    verification_sub = verification.add_subparsers(dest="action", required=True)
    for action in ("status", "check", "cancel"):
        command = verification_sub.add_parser(action)
        command.add_argument("job_id")

    config = commands.add_parser("config")
    config_sub = config.add_subparsers(dest="action", required=True)
    validate = config_sub.add_parser("validate")
    validate.add_argument("path", nargs="?")

    db = commands.add_parser("db")
    db_sub = db.add_subparsers(dest="action", required=True)
    db_sub.add_parser("migrate")

    api = commands.add_parser("api")
    api.add_argument("--host")
    api.add_argument("--port", type=int)
    ui = commands.add_parser("ui")
    ui.add_argument("--port", type=int, default=8501)
    serve = commands.add_parser("serve")
    serve.add_argument("--api-port", type=int)
    serve.add_argument("--ui-port", type=int, default=8501)
    return parser


def _remote_schedule(args, method: str, path: str, payload=None):
    gateway = _gateway(args)
    if not isinstance(gateway, ApiGateway):
        raise RuntimeError("Remote schedule helper requires --api-url")
    return gateway._request(method, path, json=payload).json() if method != "DELETE" else gateway._request(method, path)


def _handle_scrape(args) -> int:
    request = _load_mapping(args.job_config)
    gateway = _gateway(args, start_runner=bool(args.api_url))
    job = gateway.create_job(request)
    _json_print(job)
    if not args.wait:
        return 0
    if isinstance(gateway, LocalGateway):
        gateway.orchestrator.run_job(job["id"])
        job = gateway.get_job(job["id"])
    else:
        while True:
            job = gateway.get_job(job["id"])
            if job["status"] in {status.value for status in JobStatus if status.terminal} | {JobStatus.WAITING_VERIFICATION.value, JobStatus.PAUSED.value}:
                break
            time.sleep(1)
    _json_print(job)
    if job["status"] == JobStatus.WAITING_VERIFICATION.value:
        return EXIT_VERIFICATION
    if job["status"] == JobStatus.FAILED.value:
        return EXIT_FAILED
    return 0


def _handle_jobs(args) -> int:
    gateway = _gateway(args)
    if args.action == "list":
        _json_print(gateway.list_jobs(args.limit))
        return 0
    if args.action == "show":
        _json_print(gateway.get_job(args.job_id))
        return 0
    _json_print(getattr(gateway, args.action)(args.job_id))
    return 0


def _handle_export(args) -> int:
    gateway = _gateway(args)
    content, _media_type, filename = gateway.export(args.job_id, args.format, args.kind)
    destination = Path(args.output or filename).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    print(destination)
    return 0


def _handle_schedule(args) -> int:
    if args.api_url:
        if args.action == "list":
            _json_print(_remote_schedule(args, "GET", "/api/v1/schedules"))
        elif args.action == "add":
            _json_print(_remote_schedule(args, "POST", "/api/v1/schedules", _load_mapping(args.schedule_config)))
        elif args.action == "delete":
            _remote_schedule(args, "DELETE", f"/api/v1/schedules/{args.schedule_id}")
        elif args.action == "run-now":
            _json_print(_remote_schedule(args, "POST", f"/api/v1/schedules/{args.schedule_id}/run-now"))
        else:
            _json_print(_remote_schedule(
                args, "PATCH", f"/api/v1/schedules/{args.schedule_id}",
                {"enabled": args.action == "enable"},
            ))
        return 0
    gateway = _gateway(args)
    service = SchedulerService(gateway.orchestrator)
    if args.action == "list":
        _json_print([item.as_dict() for item in gateway.repository.list_schedules()])
    elif args.action == "add":
        _json_print(service.create(_load_mapping(args.schedule_config)).as_dict())
    elif args.action == "delete":
        _json_print({"deleted": service.delete(args.schedule_id)})
    elif args.action == "run-now":
        _json_print(service.run_now(args.schedule_id).as_dict())
    else:
        _json_print(service.update(args.schedule_id, {"enabled": args.action == "enable"}).as_dict())
    return 0


def _handle_verification(args) -> int:
    gateway = _gateway(args)
    if args.action == "status":
        _json_print(gateway.get_job(args.job_id))
    elif args.action == "check":
        _json_print(gateway.check_verification(args.job_id))
    else:
        _json_print(gateway.cancel(args.job_id))
    return 0


def _run_api(args) -> int:
    import uvicorn

    from speedy_scraper.api import create_app

    config = load_config(args.settings)
    host = args.host or config.api_host
    port = args.port or config.api_port
    if host not in {"127.0.0.1", "localhost", "::1"} and not config.api_key:
        raise ValueError("Set SPEEDY_SCRAPER_API_KEY before binding the API publicly.")
    uvicorn.run(create_app(args.settings), host=host, port=port, workers=1)
    return 0


def _run_ui(args) -> int:
    command = [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(args.port)]
    return subprocess.call(command, cwd=Path(__file__).resolve().parents[1])


def _run_serve(args) -> int:
    config = load_config(args.settings)
    api_port = args.api_port or config.api_port
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["SPEEDY_SCRAPER_API_URL"] = f"http://127.0.0.1:{api_port}"
    if args.settings:
        environment["SPEEDY_SCRAPER_CONFIG"] = str(
            Path(args.settings).expanduser().resolve()
        )
    api_command = [
        sys.executable, "-m", "uvicorn", "speedy_scraper.api:app_factory",
        "--factory", "--host", "127.0.0.1", "--port", str(api_port),
        "--workers", "1",
    ]
    ui_command = [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(args.ui_port)]
    api_process = subprocess.Popen(api_command, cwd=root, env=environment)
    ui_process = subprocess.Popen(ui_command, cwd=root, env=environment)

    def stop(_signum=None, _frame=None):
        for process in (ui_process, api_process):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while api_process.poll() is None and ui_process.poll() is None:
            time.sleep(0.5)
    finally:
        stop()
    return api_process.returncode or ui_process.returncode or 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scrape":
            return _handle_scrape(args)
        if args.command == "jobs":
            return _handle_jobs(args)
        if args.command == "export":
            return _handle_export(args)
        if args.command == "schedule":
            return _handle_schedule(args)
        if args.command == "verification":
            return _handle_verification(args)
        if args.command == "config":
            config = load_config(args.path or args.settings)
            _json_print({"valid": True, "database_path": config.storage.database_path})
            return 0
        if args.command == "db":
            gateway = _gateway(args)
            _json_print({"schema_version": gateway.repository.migrate(), "database_path": str(gateway.repository.path)})
            return 0
        if args.command == "api":
            return _run_api(args)
        if args.command == "ui":
            return _run_ui(args)
        if args.command == "serve":
            return _run_serve(args)
    except (ValueError, KeyError, RuntimeError, OSError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
