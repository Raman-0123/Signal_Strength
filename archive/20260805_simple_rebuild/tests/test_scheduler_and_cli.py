import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from speedy_scraper.cli import (
    EXIT_FAILED,
    EXIT_INVALID,
    EXIT_VERIFICATION,
    ApiGateway,
    _run_api,
    _run_serve,
    _run_ui,
    build_parser,
    main,
)
from speedy_scraper.config import load_catalog, load_config
from speedy_scraper.domain import JobStatus
from speedy_scraper.orchestrator import ScraperOrchestrator
from speedy_scraper.repository import LeadRepository
from speedy_scraper.scheduler import SchedulerService


class SchedulerAndCliTests(unittest.TestCase):
    def _settings(self, directory: str) -> Path:
        path = Path(directory) / "settings.yaml"
        path.write_text(
            f"storage:\n  database_path: {Path(directory) / 'service.db'}\n",
            encoding="utf-8",
        )
        return path

    def test_scheduler_persists_and_run_now_marks_request_scheduled(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self._settings(directory), env={})
            repository = LeadRepository(config.storage)
            repository.migrate()
            orchestrator = ScraperOrchestrator(config, repository, catalog=load_catalog())
            scheduler = SchedulerService(orchestrator)
            schedule = scheduler.create({
                "name": "Once",
                "workflow": "reconcile",
                "trigger": {"type": "date", "run_date": "2099-01-01T10:00:00"},
                "request": {"rows": [], "mapping": {}},
                "timezone": "Asia/Kolkata",
                "enabled": True,
            })
            self.assertEqual(repository.get_schedule(schedule.id).timezone, "Asia/Kolkata")
            job = scheduler.run_now(schedule.id)
            self.assertTrue(job.request["scheduled"])
            self.assertEqual(scheduler.run_now(schedule.id).id, job.id)
            self.assertFalse(scheduler.delete("missing"))

            with self.assertRaisesRegex(ValueError, "Date schedules require"):
                scheduler.create({
                    "name": "Bad date", "workflow": "reconcile",
                    "trigger": {"type": "date"}, "request": {},
                })
            with self.assertRaisesRegex(ValueError, "Unsupported schedule"):
                scheduler.create({
                    "name": "Bad", "workflow": "reconcile",
                    "trigger": {"type": "moon"}, "request": {},
                })

    def test_scheduler_rehydrates_updates_and_disables_one_time_run(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self._settings(directory), env={})
            repository = LeadRepository(config.storage)
            repository.migrate()
            orchestrator = ScraperOrchestrator(config, repository, catalog=load_catalog())
            scheduler = SchedulerService(orchestrator)
            interval = scheduler.create({
                "name": "Frequent", "workflow": "reconcile",
                "trigger": {"type": "interval", "hours": 2},
                "request": {"rows": [], "mapping": {}}, "enabled": True,
            })
            cron = scheduler.create({
                "name": "Cron", "workflow": "reconcile",
                "trigger": {"type": "cron", "expression": "0 3 * * *"},
                "request": {}, "enabled": True,
            })
            scheduler.start()
            self.assertIsNotNone(scheduler.scheduler.get_job(f"schedule:{interval.id}"))
            self.assertIsNotNone(scheduler.scheduler.get_job(f"schedule:{cron.id}"))
            updated = scheduler.update(interval.id, {"enabled": False, "name": "Disabled"})
            self.assertFalse(updated.enabled)
            self.assertIsNone(scheduler.scheduler.get_job(f"schedule:{interval.id}"))

            one_time = scheduler.create({
                "name": "One time", "workflow": "reconcile",
                "trigger": {"type": "date", "run_date": "2099-01-01T00:00:00"},
                "request": {}, "enabled": True,
            })
            scheduler._enqueue(one_time.id)
            self.assertFalse(repository.get_schedule(one_time.id).enabled)
            scheduler.stop()

    def test_cli_configuration_and_migration_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            self.assertEqual(
                main(["--settings", str(settings), "config", "validate"]),
                0,
            )
            self.assertEqual(
                main(["--settings", str(settings), "db", "migrate"]),
                0,
            )
            self.assertTrue((Path(directory) / "service.db").exists())

    def test_cli_local_job_export_schedule_and_verification_commands(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            settings = self._settings(directory)
            request = Path(directory) / "job.yaml"
            request.write_text(
                "workflow: reconcile\nrows:\n  - old: value\nmapping:\n  new: old\n",
                encoding="utf-8",
            )
            base = ["--settings", str(settings)]
            self.assertEqual(main([*base, "scrape", "run", "--job-config", str(request)]), 0)

            gateway_repository = LeadRepository(Path(directory) / "service.db")
            completed = gateway_repository.list_jobs(limit=10)[0]
            self.assertEqual(completed.status.value, "completed")
            self.assertEqual(main([*base, "jobs", "list", "--limit", "5"]), 0)
            self.assertEqual(main([*base, "jobs", "show", completed.id]), 0)
            output = Path(directory) / "result.json"
            self.assertEqual(main([
                *base, "export", completed.id, "--format", "json", "--output", str(output),
            ]), 0)
            self.assertTrue(output.exists())

            control = gateway_repository.create_job("reconcile", {"workflow": "reconcile"})
            self.assertEqual(main([*base, "jobs", "pause", control.id]), 0)
            self.assertEqual(main([*base, "jobs", "resume", control.id]), 0)
            self.assertEqual(main([*base, "verification", "status", control.id]), 0)
            self.assertEqual(main([*base, "verification", "check", control.id]), 0)
            self.assertEqual(main([*base, "verification", "cancel", control.id]), 0)

            failed = gateway_repository.create_job("bad", {"workflow": "bad"})
            gateway_repository.transition(failed.id, JobStatus.RUNNING)
            gateway_repository.transition(failed.id, JobStatus.FAILED)
            self.assertEqual(main([*base, "jobs", "retry", failed.id]), 0)

            schedule_config = Path(directory) / "schedule.yaml"
            schedule_config.write_text(
                "name: Every day\nworkflow: reconcile\n"
                "trigger:\n  type: interval\n  hours: 24\n"
                "request:\n  rows: []\n  mapping: {}\nenabled: true\n",
                encoding="utf-8",
            )
            self.assertEqual(main([*base, "schedule", "add", "--schedule-config", str(schedule_config)]), 0)
            schedule_id = gateway_repository.list_schedules()[0].id
            self.assertEqual(main([*base, "schedule", "list"]), 0)
            self.assertEqual(main([*base, "schedule", "disable", schedule_id]), 0)
            self.assertEqual(main([*base, "schedule", "enable", schedule_id]), 0)
            self.assertEqual(main([*base, "schedule", "run-now", schedule_id]), 0)
            self.assertEqual(main([*base, "schedule", "delete", schedule_id]), 0)

    def test_cli_remote_exit_codes_helpers_and_invalid_inputs(self):
        parser = build_parser()
        self.assertEqual(parser.prog, "speedy-scraper")
        remote = ApiGateway("http://remote")
        remote.create_job = Mock(return_value={"id": "job", "status": "queued"})
        remote.get_job = Mock(return_value={"id": "job", "status": "waiting_verification"})
        with (
            patch("speedy_scraper.cli._gateway", return_value=remote),
            patch("speedy_scraper.cli.time.sleep"),
            redirect_stdout(io.StringIO()),
        ):
            with tempfile.TemporaryDirectory() as directory:
                request = Path(directory) / "request.yaml"
                request.write_text("workflow: reconcile\n", encoding="utf-8")
                self.assertEqual(main([
                    "--api-url", "http://remote", "scrape", "run", "--job-config", str(request),
                ]), EXIT_VERIFICATION)
                remote.get_job.return_value = {"id": "job", "status": "failed"}
                self.assertEqual(main([
                    "--api-url", "http://remote", "scrape", "run", "--job-config", str(request),
                ]), EXIT_FAILED)
                self.assertEqual(main([
                    "--api-url", "http://remote", "scrape", "run", "--job-config", str(request),
                    "--no-wait",
                ]), 0)

        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            invalid = Path(directory) / "invalid.yaml"
            invalid.write_text("- not\n- a mapping\n", encoding="utf-8")
            self.assertEqual(main(["config", "validate", str(invalid)]), EXIT_INVALID)
            self.assertEqual(main(["jobs", "show", "missing"]), EXIT_INVALID)

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            args = SimpleNamespace(settings=str(settings), host="127.0.0.1", port=9999)
            with patch("uvicorn.run") as run:
                self.assertEqual(_run_api(args), 0)
                run.assert_called_once()
        with patch("speedy_scraper.cli.subprocess.call", return_value=7):
            self.assertEqual(_run_ui(SimpleNamespace(port=8502)), 7)

        process = Mock(returncode=0)
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as directory:
            serve_args = SimpleNamespace(
                settings=str(self._settings(directory)), api_port=9998, ui_port=8503,
            )
            with (
                patch("speedy_scraper.cli.subprocess.Popen", return_value=process),
                patch("speedy_scraper.cli.signal.signal"),
            ):
                self.assertEqual(_run_serve(serve_args), 0)


if __name__ == "__main__":
    unittest.main()
