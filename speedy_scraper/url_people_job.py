from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from speedy_scraper.background_jobs import (
    JobHeartbeat,
    read_json,
    stop_requested,
    update_status,
    write_json,
)
from speedy_scraper.event_speakers import (
    EventSpeaker,
    count_status,
    enrich_speaker,
    extract_people_records,
    fetch_public_html,
    write_speaker_exports,
)
from speedy_scraper.linkedin import normalize_linkedin_url
from speedy_scraper.models import DEFAULT_SOURCE_NAMES
from speedy_scraper.pipeline import load_existing_people_keys, load_existing_urls
from speedy_scraper.sources import (
    SearchSource,
    build_sources,
    close_sources,
    configure_google_challenge_wait,
)
from speedy_scraper.text import normalize_text

Fetcher = Callable[[str], str]
SourceBuilder = Callable[[list[str]], list[SearchSource]]


def run_url_people_job(
    job_dir: Path | str,
    *,
    fetcher: Fetcher | None = None,
    source_builder: SourceBuilder | None = None,
) -> list[EventSpeaker]:
    path = Path(job_dir)
    config = read_json(path / "config.json", default={})
    if not isinstance(config, dict):
        raise ValueError("Invalid URL people job config")
    fetch = fetcher or fetch_public_html
    build = source_builder or build_sources
    checkpoint_path = path / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, default={})
    search_sources: list[SearchSource] = []
    try:
        if isinstance(checkpoint, dict) and checkpoint.get("speakers"):
            speakers = _unique_speakers([_speaker_from_data(item) for item in checkpoint["speakers"]])
            next_index = int(checkpoint.get("next_index") or 0)
        else:
            # Support both a single URL and a list of URLs
            raw_urls = config.get("source_urls") or config.get("source_url") or ""
            if isinstance(raw_urls, str):
                source_urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
            else:
                source_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
            if not source_urls:
                raise ValueError("No source URL(s) provided in job config.")

            # Extract from each URL and deduplicate by name+company
            existing_files = [Path(item) for item in _string_list(config.get("existing_files"))]
            existing_urls = load_existing_urls(existing_files)
            existing_people = load_existing_people_keys(existing_files)
            seen_people: set[str] = set()
            speakers: list[EventSpeaker] = []
            for url in source_urls:
                try:
                    page_html = fetch(url)
                    page_speakers = extract_people_records(page_html, url)
                except Exception as exc:
                    update_status(
                        path,
                        state="running",
                        workflow="url_people",
                        job_id=path.name,
                        processed=0,
                        total=0,
                        message=f"Warning: could not parse {url}: {exc}",
                    )
                    continue
                for sp in page_speakers:
                    key = f"{normalize_text(sp.name)}|{normalize_text(sp.company)}"
                    profile_url = normalize_linkedin_url(sp.linkedin_url)
                    if (
                        key not in seen_people
                        and key not in existing_people
                        and profile_url not in existing_urls
                    ):
                        seen_people.add(key)
                        speakers.append(sp)

            if not speakers:
                raise ValueError(
                    "No speakers could be extracted from any of the provided URLs."
                )
            next_index = 0
            _save_checkpoint(checkpoint_path, speakers, next_index)

        total = len(speakers)
        search_sources = configure_google_challenge_wait(
            build(_string_list(config.get("sources")) or list(DEFAULT_SOURCE_NAMES)),
            int(config.get("google_manual_challenge_seconds") or 0),
        )
        enrich_missing = bool(config.get("enrich_missing", True))
        include_terms = _string_list(config.get("include_terms"))
        exclude_terms = _string_list(config.get("exclude_terms"))
        captcha_required = False

        def _source_progress(event: dict[str, object]) -> None:
            nonlocal captcha_required
            if event.get("event") in {"captcha_required", "source_error"}:
                captcha_required = True
                update_status(
                    path,
                    captcha_required=True,
                    captcha_source=str(event.get("source") or ""),
                    fallback_recommended=True,
                    message=(
                        f"{event.get('source') or 'A search source'} failed or was challenged. "
                        "Google browser is the recommended fallback; pause and resume visibly if needed."
                    ),
                )
        _status(path, "running", speakers, next_index, total, "Enrichment running")

        for index in range(next_index, total):
            if stop_requested(path):
                _pause(path, speakers, index, total)
                return speakers
            speaker = speakers[index]
            if enrich_missing and speaker.match_status != "provided":
                with JobHeartbeat(
                    path,
                    activity="Searching public results for the current person",
                    current_name=speaker.name,
                    current_company=speaker.company,
                ):
                    speakers[index] = enrich_speaker(
                        speaker,
                        search_sources,
                        headless=bool(config.get("browser_headless", True)),
                        include_terms=include_terms,
                        exclude_terms=exclude_terms,
                        on_source_error=_source_progress,
                    )
            next_index = index + 1
            _save_checkpoint(checkpoint_path, speakers, next_index)
            _status(
                path,
                "running",
                speakers,
                next_index,
                total,
                f"Processed {next_index}/{total}: {speaker.name}",
                current_name=speaker.name,
                captcha_required=captcha_required,
            )

        csv_path, xlsx_path = write_speaker_exports(speakers, path / "URL_People_LinkedIn")
        _status(
            path,
            "completed",
            speakers,
            total,
            total,
            "Completed",
            csv_path=str(csv_path),
            xlsx_path=str(xlsx_path),
        )
        return speakers
    except Exception as exc:
        current = read_json(checkpoint_path, default={})
        processed = int(current.get("next_index") or 0) if isinstance(current, dict) else 0
        total = len(current.get("speakers") or []) if isinstance(current, dict) else 0
        update_status(
            path,
            state="failed",
            workflow="url_people",
            job_id=path.name,
            processed=processed,
            total=total,
            message=str(exc),
        )
        raise
    finally:
        close_sources(search_sources)


def load_checkpoint_speakers(job_dir: Path | str) -> tuple[list[EventSpeaker], int]:
    checkpoint = read_json(Path(job_dir) / "checkpoint.json", default={})
    if not isinstance(checkpoint, dict):
        return [], 0
    speakers = _unique_speakers([_speaker_from_data(item) for item in checkpoint.get("speakers") or []])
    return speakers, int(checkpoint.get("next_index") or 0)


def _pause(path: Path, speakers: list[EventSpeaker], next_index: int, total: int) -> None:
    _save_checkpoint(path / "checkpoint.json", speakers, next_index)
    csv_path, xlsx_path = write_speaker_exports(speakers, path / "URL_People_LinkedIn_partial")
    _status(
        path,
        "paused",
        speakers,
        next_index,
        total,
        f"Paused safely after {next_index}/{total} people",
        csv_path=str(csv_path),
        xlsx_path=str(xlsx_path),
    )


def _status(
    path: Path,
    state: str,
    speakers: list[EventSpeaker],
    processed: int,
    total: int,
    message: str,
    **extra: object,
) -> None:
    update_status(
        path,
        state=state,
        workflow="url_people",
        job_id=path.name,
        processed=processed,
        total=total,
        pending=max(total - processed, 0),
        provided=count_status(speakers, "provided"),
        matched=count_status(speakers, "matched"),
        ambiguous=count_status(speakers, "ambiguous"),
        not_found=count_status(speakers, "not_found"),
        message=message,
        **extra,
    )


def _save_checkpoint(path: Path, speakers: list[EventSpeaker], next_index: int) -> None:
    speakers = _unique_speakers(speakers)
    write_json(
        path,
        {
            "version": 1,
            "next_index": next_index,
            "speakers": [_speaker_to_data(speaker) for speaker in speakers],
        },
    )


def _unique_speakers(speakers: list[EventSpeaker]) -> list[EventSpeaker]:
    unique: dict[str, EventSpeaker] = {}
    for speaker in speakers:
        canonical = normalize_linkedin_url(speaker.linkedin_url)
        key = canonical or f"{normalize_text(speaker.name)}|{normalize_text(speaker.company)}"
        current = unique.get(key)
        if current is None or speaker.confidence > current.confidence:
            unique[key] = speaker
    return list(unique.values())


def _speaker_to_data(speaker: EventSpeaker) -> dict[str, object]:
    return {
        "speaker_id": speaker.speaker_id,
        "name": speaker.name,
        "designation": speaker.designation,
        "company": speaker.company,
        "country": speaker.country,
        "linkedin_url": speaker.linkedin_url,
        "match_status": speaker.match_status,
        "confidence": speaker.confidence,
        "match_evidence": speaker.match_evidence,
        "source_url": speaker.source_url,
    }


def _speaker_from_data(value: dict[str, Any]) -> EventSpeaker:
    return EventSpeaker(
        speaker_id=str(value.get("speaker_id") or ""),
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        country=str(value.get("country") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        match_status=str(value.get("match_status") or "not_found"),
        confidence=float(value.get("confidence") or 0.0),
        match_evidence=str(value.get("match_evidence") or ""),
        source_url=str(value.get("source_url") or ""),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    run_url_people_job(args.job_dir)


if __name__ == "__main__":
    main()
