from __future__ import annotations

import argparse
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from speedy_scraper.background_jobs import (
    JobHeartbeat,
    read_json,
    stop_requested,
    update_status,
    write_json,
)
from speedy_scraper.linkedin import linkedin_id, normalize_linkedin_url
from speedy_scraper.models import DEFAULT_SOURCE_NAMES
from speedy_scraper.parser import candidates_from_results
from speedy_scraper.sources import (
    SearchSource,
    SourceError,
    build_sources,
    close_sources,
    configure_google_challenge_wait,
)
from speedy_scraper.taxonomy import resolve_role
from speedy_scraper.text import clean_spaces, normalize_text, or_group, unique_terms
from speedy_scraper.validator import (
    company_match_strength,
    company_matches,
    role_match_strength,
    role_matches,
)


@dataclass(frozen=True)
class CompanyPoc:
    name: str
    designation: str
    company: str
    linkedin_url: str
    source: str
    confidence: int
    evidence: str
    requested_company: str
    requested_designation: str

    def as_row(self) -> dict[str, object]:
        return {
            "Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "LinkedIn ID": linkedin_id(self.linkedin_url),
            "LinkedIn URL": self.linkedin_url,
            "Source": self.source,
            "Confidence": self.confidence,
            "Match Evidence": self.evidence,
            "Requested Company": self.requested_company,
            "Requested Designation": self.requested_designation,
        }


def build_company_poc_tasks(companies: list[str], designations: list[str], locations: list[str] = None) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    locs = unique_terms(locations) if locations else [""]
    for company in unique_terms(companies):
        for designation in unique_terms(designations):
            for loc in locs:
                role_clause = or_group(_role_aliases(designation))
                loc_clause = f' "{_safe_quote(loc)}"' if loc else ""
                tasks.append(
                    {
                        "company": company,
                        "designation": designation,
                        "location": loc,
                        "query": f'site:linkedin.com/in "{_safe_quote(company)}" {role_clause}{loc_clause}',
                    }
                )
    return tasks


def run_company_poc_job(
    job_dir: Path | str,
    *,
    source_builder=None,
) -> list[CompanyPoc]:
    path = Path(job_dir)
    config = read_json(path / "config.json", default={})
    if not isinstance(config, dict):
        raise ValueError("Invalid company POC job config")
    checkpoint_path = path / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, default={})
    tasks = build_company_poc_tasks(
        _strings(config.get("companies")), 
        _strings(config.get("designations")),
        _strings(config.get("locations"))
    )
    if not tasks:
        raise ValueError("Enter at least one company and one designation")
    sources: list[SearchSource] = configure_google_challenge_wait(
        (source_builder or build_sources)(
            _strings(config.get("sources")) or list(DEFAULT_SOURCE_NAMES)
        ),
        int(config.get("google_manual_challenge_seconds") or 0),
    )
    target_count = max(1, int(config.get("target_count") or 100))
    max_results = max(1, int(config.get("max_results_per_search") or 20))
    headless = bool(config.get("browser_headless", True))
    pocs = (
        filter_company_pocs([_poc_from_data(item) for item in checkpoint.get("pocs", [])])
        if checkpoint
        else []
    )
    rejections = list(checkpoint.get("rejections", [])) if checkpoint else []
    errors = list(checkpoint.get("errors", [])) if checkpoint else []
    task_index = int(checkpoint.get("task_index") or 0) if checkpoint else 0
    source_index = int(checkpoint.get("source_index") or 0) if checkpoint else 0
    seen = {poc.linkedin_url for poc in pocs}
    try:
        _write_poc_status(
            path,
            "running",
            pocs,
            task_index,
            len(tasks),
            "POC search running",
            searches_completed=task_index * len(sources) + source_index,
            searches_total=len(tasks) * len(sources),
        )
        while task_index < len(tasks) and len(pocs) < target_count:
            if stop_requested(path):
                _save_poc_checkpoint(
                    checkpoint_path, pocs, rejections, errors, task_index, source_index
                )
                csv_path, xlsx_path = write_company_poc_exports(
                    pocs, rejections, path / "Company_Designation_POCs_partial"
                )
                _write_poc_status(
                    path,
                    "paused",
                    pocs,
                    task_index,
                    len(tasks),
                    "Paused safely; completed searches will not repeat",
                    csv_path=str(csv_path),
                    xlsx_path=str(xlsx_path),
                )
                return pocs

            task = tasks[task_index]
            source = sources[source_index]
            searches_completed = task_index * len(sources) + source_index
            _write_poc_status(
                path,
                "running",
                pocs,
                task_index,
                len(tasks),
                f"Searching {task['company']} · {task['designation']} · {source.name}",
                searches_completed=searches_completed,
                searches_total=len(tasks) * len(sources),
                current_company=task["company"],
                current_designation=task["designation"],
                current_source=source.name,
                current_query=task["query"],
            )
            try:
                with JobHeartbeat(
                    path,
                    activity="Public search request in progress",
                    current_company=task["company"],
                    current_designation=task["designation"],
                    current_source=source.name,
                ):
                    results = source.search(
                        task["query"], max_results=max_results, headless=headless
                    )
            except SourceError as exc:
                message = f"{source.name}: {exc}"
                if message not in errors:
                    errors.append(message)
                results = []
            for candidate in candidates_from_results(results):
                canonical = normalize_linkedin_url(candidate.linkedin_url)
                if not canonical or canonical in seen:
                    continue
                poc, reason = _match_candidate(candidate, task["company"], task["designation"])
                if poc:
                    pocs.append(poc)
                    seen.add(canonical)
                    if len(pocs) >= target_count:
                        break
                else:
                    rejections.append(
                        {
                            "Name": candidate.name,
                            "LinkedIn URL": canonical,
                            "Requested Company": task["company"],
                            "Requested Designation": task["designation"],
                            "Reason": reason,
                        }
                    )

            source_index += 1
            if source_index >= len(sources):
                source_index = 0
                task_index += 1
            _save_poc_checkpoint(checkpoint_path, pocs, rejections, errors, task_index, source_index)
            _write_poc_status(
                path,
                "running",
                pocs,
                task_index,
                len(tasks),
                f"Found {len(pocs)} POCs · completed {task_index}/{len(tasks)} query groups",
                searches_completed=task_index * len(sources) + source_index,
                searches_total=len(tasks) * len(sources),
                current_company=task["company"],
                current_designation=task["designation"],
                current_source=source.name,
            )

        csv_path, xlsx_path = write_company_poc_exports(
            pocs, rejections, path / "Company_Designation_POCs"
        )
        _write_poc_status(
            path,
            "completed",
            pocs,
            task_index,
            len(tasks),
            f"Completed with {len(pocs)} matched POCs",
            csv_path=str(csv_path),
            xlsx_path=str(xlsx_path),
        )
        return pocs
    except Exception as exc:
        _save_poc_checkpoint(checkpoint_path, pocs, rejections, errors, task_index, source_index)
        _write_poc_status(path, "failed", pocs, task_index, len(tasks), str(exc))
        raise
    finally:
        close_sources(sources)


def load_company_poc_checkpoint(job_dir: Path | str) -> tuple[list[CompanyPoc], list[dict[str, str]]]:
    value = read_json(Path(job_dir) / "checkpoint.json", default={})
    if not isinstance(value, dict):
        return [], []
    pocs = filter_company_pocs([_poc_from_data(item) for item in value.get("pocs", [])])
    return pocs, list(value.get("rejections", []))


def filter_company_pocs(pocs: list[CompanyPoc]) -> list[CompanyPoc]:
    return [
        poc
        for poc in pocs
        if role_matches(poc.designation, [poc.requested_designation])
        and company_matches(poc.company, poc.requested_company)
    ]


def company_pocs_frame(pocs: list[CompanyPoc]) -> pd.DataFrame:
    columns = [
        "Name",
        "Designation",
        "Company",
        "LinkedIn ID",
        "LinkedIn URL",
        "Source",
        "Confidence",
        "Match Evidence",
        "Requested Company",
        "Requested Designation",
    ]
    return pd.DataFrame([poc.as_row() for poc in pocs], columns=columns)


def write_company_poc_exports(
    pocs: list[CompanyPoc], rejections: list[dict[str, str]], output_base: Path | str
) -> tuple[Path, Path]:
    base = Path(output_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    csv_path = base.with_suffix(".csv")
    xlsx_path = base.with_suffix(".xlsx")
    frame = company_pocs_frame(pocs)
    frame.to_csv(csv_path, index=False)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Matched POCs", index=False)
        pd.DataFrame(rejections).to_excel(writer, sheet_name="Rejected", index=False)
    return csv_path, xlsx_path


def _match_candidate(candidate, company: str, designation: str) -> tuple[CompanyPoc | None, str]:
    name = clean_spaces(candidate.name)
    if not _valid_name(name):
        return None, "invalid_name"
    parsed_company = clean_spaces(candidate.company)
    company_strength = company_match_strength(parsed_company, company)
    if not company_strength:
        return None, "company_mismatch"
    parsed_designation = clean_spaces(candidate.designation)
    role_strength = role_match_strength(parsed_designation, designation)
    if not role_strength:
        return None, "designation_mismatch"
    evidence = clean_spaces(f"{candidate.title} {candidate.body}")[:700]
    confidence = 68 + company_strength * 4 + role_strength * 4
    if len(candidate.sources_seen) > 1:
        confidence += 4
    return (
        CompanyPoc(
            name=name,
            designation=parsed_designation,
            company=parsed_company,
            linkedin_url=normalize_linkedin_url(candidate.linkedin_url),
            source=", ".join(sorted(candidate.sources_seen)) or candidate.source,
            confidence=min(confidence, 96),
            evidence=evidence,
            requested_company=company,
            requested_designation=designation,
        ),
        "",
    )


def _valid_name(value: str) -> bool:
    key = normalize_text(value)
    return (
        key not in {"", "unknown", "linkedin member", "new cio", "new cto", "new cdo"}
        and 2 <= len(key.split()) <= 8
        and not bool(re.search(r"\d|@", value))
    )


def _role_aliases(designation: str) -> list[str]:
    return resolve_role(designation).terms


def _safe_quote(value: str) -> str:
    return clean_spaces(value).replace('"', "'")


def _save_poc_checkpoint(
    path: Path,
    pocs: list[CompanyPoc],
    rejections: list[dict[str, str]],
    errors: list[str],
    task_index: int,
    source_index: int,
) -> None:
    write_json(
        path,
        {
            "version": 1,
            "task_index": task_index,
            "source_index": source_index,
            "pocs": [asdict(poc) for poc in pocs],
            "rejections": rejections,
            "errors": errors,
        },
    )


def _write_poc_status(
    path: Path,
    state: str,
    pocs: list[CompanyPoc],
    processed: int,
    total: int,
    message: str,
    **extra: object,
) -> None:
    update_status(
        path,
        state=state,
        workflow="company_pocs",
        job_id=path.name,
        processed=processed,
        total=total,
        matched=len(pocs),
        message=message,
        **extra,
    )


def _poc_from_data(value: dict[str, Any]) -> CompanyPoc:
    return CompanyPoc(
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        source=str(value.get("source") or ""),
        confidence=int(value.get("confidence") or 0),
        evidence=str(value.get("evidence") or ""),
        requested_company=str(value.get("requested_company") or ""),
        requested_designation=str(value.get("requested_designation") or ""),
    )


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_spaces(str(item)) for item in value if clean_spaces(str(item))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    run_company_poc_job(args.job_dir)


if __name__ == "__main__":
    main()
