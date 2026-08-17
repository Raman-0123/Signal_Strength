from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from speedy_scraper.background_jobs import JobHeartbeat, read_json, update_status, write_json
from speedy_scraper.outreach_intelligence import (
    OutreachRun,
    SourceRole,
    SourceSpec,
    canonicalize_frame,
    outreach_run_from_dict,
    outreach_run_to_dict,
    read_source_sheets,
    run_outreach_match,
    write_outreach_exports,
)

CHECKPOINT_VERSION = 1


def run_outreach_job(job_dir: Path | str) -> OutreachRun:
    path = Path(job_dir)
    update_status(
        path,
        state="running",
        workflow="outreach_intelligence",
        job_id=path.name,
        phase="load",
        message="Reading uploaded outreach sources",
        processed=0,
        total=0,
    )
    try:
        raw_config = read_json(path / "config.json", default={})
        if not isinstance(raw_config, dict):
            raise ValueError("Invalid Outreach Intelligence job config")
        primary_data = raw_config.get("primary")
        previous_data = raw_config.get("previous")
        if not isinstance(primary_data, dict):
            raise ValueError("A primary prospect source is required")
        if not isinstance(previous_data, list) or not previous_data:
            raise ValueError("At least one previous-outreach source is required")
        source_payloads = [primary_data, *previous_data]
        total = len(source_payloads) + 2
        default_region = str(raw_config.get("default_phone_region") or "IN").upper()
        status_map = {
            str(key): str(value)
            for key, value in dict(raw_config.get("status_map") or {}).items()
        }

        primary_records = []
        previous_records = []
        invalid_rows = []
        with JobHeartbeat(path, activity="Normalizing outreach source rows"):
            for index, payload in enumerate(source_payloads, start=1):
                if not isinstance(payload, dict):
                    raise ValueError("Invalid source configuration")
                spec = _source_spec(payload)
                sheets = read_source_sheets(spec.path)
                if spec.sheet_name not in sheets:
                    raise ValueError(
                        f"{spec.source_name}: sheet {spec.sheet_name!r} is no longer available"
                    )
                records, source_invalid = canonicalize_frame(
                    sheets[spec.sheet_name],
                    spec,
                    default_phone_region=default_region,
                )
                if spec.role == SourceRole.PRIMARY:
                    primary_records.extend(records)
                else:
                    previous_records.extend(records)
                invalid_rows.extend(source_invalid)
                update_status(
                    path,
                    state="running",
                    workflow="outreach_intelligence",
                    job_id=path.name,
                    phase="normalize",
                    message=f"Normalized {spec.source_name} · {spec.sheet_name}",
                    processed=index,
                    total=total,
                    primary=len(primary_records),
                    previous=len(previous_records),
                    invalid=len(invalid_rows),
                )
        if not primary_records:
            raise ValueError("The selected primary source has no valid named prospects")
        if not previous_records:
            raise ValueError("The previous-outreach sources have no valid named records")

        update_status(
            path,
            state="running",
            workflow="outreach_intelligence",
            job_id=path.name,
            phase="match",
            message="Matching exact identifiers and conservative fuzzy candidates",
            processed=len(source_payloads),
            total=total,
        )
        with JobHeartbeat(path, activity="Resolving identities and outreach history"):
            run = run_outreach_match(
                primary_records,
                previous_records,
                status_map=status_map,
                invalid_rows=invalid_rows,
                config={
                    "default_phone_region": default_region,
                    "source_count": len(source_payloads),
                    "primary_source": str(primary_data.get("source_name") or ""),
                    "primary_mapping": dict(primary_data.get("mapping") or {}),
                    "previous_source_count": len(previous_data),
                    "previous_mappings": {
                        str(item.get("source_name") or ""): dict(item.get("mapping") or {})
                        for item in previous_data
                        if isinstance(item, dict)
                    },
                },
            )
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "phase": "matched",
            "run": outreach_run_to_dict(run),
        }
        write_json(path / "checkpoint.json", checkpoint)

        update_status(
            path,
            state="running",
            workflow="outreach_intelligence",
            job_id=path.name,
            phase="export",
            message="Writing safe, common, review, combined, and rejected exports",
            processed=len(source_payloads) + 1,
            total=total,
            **run.metrics,
        )
        outputs = write_outreach_exports(run, path, status_map=status_map)
        checkpoint["phase"] = "completed"
        checkpoint["outputs"] = outputs
        write_json(path / "checkpoint.json", checkpoint)
        update_status(
            path,
            state="completed",
            workflow="outreach_intelligence",
            job_id=path.name,
            phase="export",
            message=(
                f"Completed with {run.metrics.get('safe_to_contact', 0)} safe prospects and "
                f"{run.metrics.get('common_people', 0)} confirmed common people"
            ),
            processed=total,
            total=total,
            outputs=outputs,
            **run.metrics,
        )
        return run
    except Exception as exc:
        update_status(
            path,
            state="failed",
            workflow="outreach_intelligence",
            job_id=path.name,
            phase="failed",
            message=str(exc),
        )
        raise


def load_outreach_checkpoint(job_dir: Path | str) -> tuple[OutreachRun | None, dict[str, Any]]:
    checkpoint = read_json(Path(job_dir) / "checkpoint.json", default={})
    if not isinstance(checkpoint, dict):
        return None, {}
    run_data = checkpoint.get("run")
    if not isinstance(run_data, dict):
        return None, checkpoint
    return outreach_run_from_dict(run_data), checkpoint


def _source_spec(value: dict[str, Any]) -> SourceSpec:
    mapping = value.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError(f"{value.get('source_name') or 'Source'} has no column mapping")
    return SourceSpec(
        source_id=str(value.get("source_id") or ""),
        source_name=str(value.get("source_name") or ""),
        path=str(value.get("path") or ""),
        sheet_name=str(value.get("sheet_name") or ""),
        role=SourceRole(str(value.get("role") or "")),
        mapping={str(key): str(item) for key, item in mapping.items() if item},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an Outreach Intelligence comparison")
    parser.add_argument("--job-dir", required=True)
    args = parser.parse_args()
    run_outreach_job(args.job_dir)


if __name__ == "__main__":
    main()
