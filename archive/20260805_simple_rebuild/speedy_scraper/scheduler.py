"""APScheduler adapter backed by the application's schedules table."""

from __future__ import annotations

from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from speedy_scraper.domain import ScheduleRecord, utc_now


class SchedulerService:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.repository = orchestrator.repository
        self.config = orchestrator.config
        self.scheduler = BackgroundScheduler(
            timezone=ZoneInfo(self.config.scheduler.timezone),
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": self.config.scheduler.misfire_grace_seconds,
            },
        )

    def _trigger(self, definition: dict[str, Any], timezone: str):
        kind = str(definition.get("type", "cron")).lower()
        tz = ZoneInfo(timezone)
        if kind == "cron":
            if expression := definition.get("expression"):
                return CronTrigger.from_crontab(str(expression), timezone=tz)
            fields = {key: value for key, value in definition.items() if key not in {"type", "expression"}}
            return CronTrigger(timezone=tz, **fields)
        if kind == "interval":
            fields = {key: value for key, value in definition.items() if key != "type"}
            return IntervalTrigger(timezone=tz, **fields)
        if kind == "date":
            run_date = definition.get("run_date")
            if not run_date:
                raise ValueError("Date schedules require run_date")
            return DateTrigger(run_date=run_date, timezone=tz)
        raise ValueError(f"Unsupported schedule trigger: {kind}")

    def _enqueue(self, schedule_id: str) -> None:
        schedule = self.repository.get_schedule(schedule_id)
        if not schedule.enabled:
            return
        if schedule.last_job_id:
            try:
                previous = self.repository.get_job(schedule.last_job_id)
                if not previous.status.terminal and previous.status.value != "paused":
                    return
            except KeyError:
                pass
        request = {**schedule.request, "workflow": schedule.workflow, "scheduled": True}
        job = self.orchestrator.create_job(request)
        enabled = schedule.enabled and schedule.trigger.get("type", "cron") != "date"
        updated = ScheduleRecord(
            **{
                **schedule.as_dict(), "last_job_id": job.id,
                "enabled": enabled, "updated_at": utc_now(),
            },
        )
        self.repository.upsert_schedule(updated)

    def sync_schedule(self, schedule: ScheduleRecord) -> None:
        scheduler_id = f"schedule:{schedule.id}"
        try:
            self.scheduler.remove_job(scheduler_id)
        except Exception:
            pass
        if not schedule.enabled:
            return
        trigger = self._trigger(schedule.trigger, schedule.timezone)
        self.scheduler.add_job(
            self._enqueue,
            trigger=trigger,
            id=scheduler_id,
            args=[schedule.id],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.config.scheduler.misfire_grace_seconds,
        )
        next_run = self.scheduler.get_job(scheduler_id).next_run_time
        if next_run:
            self.repository.upsert_schedule(ScheduleRecord(
                **{**schedule.as_dict(), "next_run_at": next_run.isoformat()},
            ))

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        for schedule in self.repository.list_schedules(enabled_only=True):
            self.sync_schedule(schedule)

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def create(self, value: dict[str, Any]) -> ScheduleRecord:
        schedule = ScheduleRecord(
            id=str(value.get("id") or uuid4()),
            name=str(value.get("name") or "Scheduled scrape"),
            workflow=str(value.get("workflow", "lead")),
            trigger=dict(value.get("trigger", {})),
            request=dict(value.get("request", {})),
            timezone=str(value.get("timezone") or self.config.scheduler.timezone),
            enabled=bool(value.get("enabled", True)),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self._trigger(schedule.trigger, schedule.timezone)
        schedule = self.repository.upsert_schedule(schedule)
        if self.scheduler.running:
            self.sync_schedule(schedule)
        return schedule

    def update(self, schedule_id: str, value: dict[str, Any]) -> ScheduleRecord:
        current = self.repository.get_schedule(schedule_id)
        merged = current.as_dict()
        merged.update(value)
        merged["id"] = schedule_id
        merged["updated_at"] = utc_now()
        schedule = ScheduleRecord(**merged)
        self._trigger(schedule.trigger, schedule.timezone)
        schedule = self.repository.upsert_schedule(schedule)
        if self.scheduler.running:
            self.sync_schedule(schedule)
        return schedule

    def delete(self, schedule_id: str) -> bool:
        try:
            self.scheduler.remove_job(f"schedule:{schedule_id}")
        except Exception:
            pass
        return self.repository.delete_schedule(schedule_id)

    def run_now(self, schedule_id: str):
        schedule = self.repository.get_schedule(schedule_id)
        if schedule.last_job_id:
            try:
                previous = self.repository.get_job(schedule.last_job_id)
                if not previous.status.terminal and previous.status.value != "paused":
                    return previous
            except KeyError:
                pass
        request = {**schedule.request, "workflow": schedule.workflow, "scheduled": True}
        job = self.orchestrator.create_job(request)
        self.repository.upsert_schedule(ScheduleRecord(
            **{**schedule.as_dict(), "last_job_id": job.id, "updated_at": utc_now()},
        ))
        return job
