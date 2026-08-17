from __future__ import annotations

import io
import re
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import phonenumbers
from rapidfuzz import fuzz

from speedy_scraper.background_jobs import read_json, write_json
from speedy_scraper.linkedin import normalize_linkedin_url


class SourceRole(StrEnum):
    PRIMARY = "PRIMARY"
    PREVIOUS = "PREVIOUS"


class MatchType(StrEnum):
    EXACT_PHONE = "EXACT_PHONE"
    EXACT_EMAIL = "EXACT_EMAIL"
    EXACT_LINKEDIN = "EXACT_LINKEDIN"
    NAME_COMPANY_MATCH = "NAME_COMPANY_MATCH"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    NO_MATCH = "NO_MATCH"


class OutreachDecision(StrEnum):
    SAFE_TO_CONTACT = "SAFE TO CONTACT"
    ALREADY_CONTACTED = "ALREADY CONTACTED"
    DO_NOT_CONTACT = "DO NOT CONTACT"
    REVIEW_REQUIRED = "REVIEW REQUIRED"
    POSSIBLE_MATCH = "POSSIBLE MATCH"


class StatusCategory(StrEnum):
    CONTACTED = "CONTACTED"
    POSITIVE_RESPONSE = "POSITIVE_RESPONSE"
    NEGATIVE_RESPONSE = "NEGATIVE_RESPONSE"
    NO_RESPONSE = "NO_RESPONSE"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    UNKNOWN = "UNKNOWN"
    SAFE_OR_RECONTACT_ALLOWED = "SAFE_OR_RECONTACT_ALLOWED"
    REVIEW = "REVIEW"


class ReviewAction(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    CONFIRM_MATCH = "CONFIRM_MATCH"
    REJECT_MATCH = "REJECT_MATCH"
    MARK_SAFE = "MARK_SAFE"
    MARK_DO_NOT_CONTACT = "MARK_DO_NOT_CONTACT"


CANONICAL_FIELDS = (
    "full_name",
    "first_name",
    "last_name",
    "company",
    "designation",
    "email",
    "phone",
    "linkedin_url",
    "status",
    "response",
    "notes",
    "owner",
    "contact_date",
)

FIELD_LABELS = {
    "full_name": "Full name",
    "first_name": "First name",
    "last_name": "Last name",
    "company": "Company",
    "designation": "Designation",
    "email": "Email",
    "phone": "Phone",
    "linkedin_url": "LinkedIn URL",
    "status": "Previous outreach status",
    "response": "Previous response",
    "notes": "Notes",
    "owner": "Owner / contacted by",
    "contact_date": "Contact date",
}

_ALIASES = {
    "full_name": {
        "name",
        "full name",
        "poc",
        "poc name",
        "contact",
        "contact name",
        "contact person",
        "person",
        "lead name",
        "prospect",
        "attendee",
        "delegate",
        "speaker",
    },
    "first_name": {"first name", "given name", "forename"},
    "last_name": {"last name", "surname", "family name"},
    "company": {"company", "organization", "organisation", "employer", "account", "firm"},
    "designation": {"designation", "title", "job title", "role", "position"},
    "email": {"email", "email address", "work email", "business email", "e mail"},
    "phone": {
        "phone",
        "phone number",
        "mobile",
        "mobile number",
        "contact number",
        "telephone",
        "whatsapp",
    },
    "linkedin_url": {
        "linkedin",
        "linkedin url",
        "linkedin profile",
        "profile url",
        "person linkedin",
    },
    "status": {
        "status",
        "outreach status",
        "poc status",
        "contact status",
        "response status",
    },
    "response": {"response", "poc response", "reply", "outcome", "feedback"},
    "notes": {"notes", "comments", "remarks", "description"},
    "owner": {
        "owner",
        "contacted by",
        "sdr",
        "bdr",
        "colleague",
        "relationship owner",
    },
    "contact_date": {
        "contact date",
        "outreach date",
        "last contacted",
        "date",
        "last activity",
    },
}

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_COMPANY_SUFFIXES = (
    "private limited",
    "pvt limited",
    "pvt ltd",
    "limited liability partnership",
    "llp",
    "limited",
    "ltd",
    "incorporated",
    "inc",
    "llc",
    "plc",
)


@dataclass(frozen=True)
class ColumnDetection:
    mapping: dict[str, str]
    ambiguous: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    source_name: str
    path: str
    sheet_name: str
    role: SourceRole
    mapping: dict[str, str]


@dataclass
class CanonicalRecord:
    record_id: str
    source_id: str
    source_name: str
    sheet_name: str
    source_role: SourceRole
    row_number: int
    full_name: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    designation: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    status: str = ""
    response: str = ""
    notes: str = ""
    owner: str = ""
    contact_date: str = ""
    normalized_name: str = ""
    normalized_company: str = ""
    normalized_email: str = ""
    normalized_phone: str = ""
    normalized_linkedin: str = ""
    raw: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InvalidRow:
    source_id: str
    source_name: str
    sheet_name: str
    row_number: int
    reason: str
    disposition: str
    duplicate_of: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class MatchResult:
    primary_id: str
    previous_ids: list[str]
    match_type: MatchType
    confidence: int
    matched_fields: list[str]
    explanation: str
    decision: OutreachDecision
    confirmed: bool


@dataclass
class OutreachRun:
    primary_records: list[CanonicalRecord]
    previous_records: list[CanonicalRecord]
    matches: list[MatchResult]
    invalid_rows: list[InvalidRow]
    metrics: dict[str, int]
    config: dict[str, Any] = field(default_factory=dict)


def normalize_identity_text(value: object) -> str:
    text = unicodedata.normalize(
        "NFKD", _display_value(value).casefold().replace("&", " and ")
    )
    normalized: list[str] = []
    for character in text:
        if unicodedata.combining(character):
            continue
        normalized.append(character if character.isalnum() else " ")
    return " ".join("".join(normalized).split())


def normalize_company(value: object) -> str:
    text = normalize_identity_text(value)
    changed = True
    while text and changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if text == suffix:
                return ""
            if text.endswith(f" {suffix}"):
                text = text[: -len(suffix)].strip()
                changed = True
                break
    return text


def normalize_email(value: object) -> str:
    email = _display_value(value).strip().casefold()
    return email if _EMAIL_PATTERN.fullmatch(email) else ""


def normalize_phone(value: object, default_region: str = "IN") -> str:
    raw = _display_value(value)
    if not raw:
        return ""
    try:
        number = phonenumbers.parse(raw, default_region.upper())
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_possible_number(number) or not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def normalize_header(value: object) -> str:
    return normalize_identity_text(value)


def detect_columns(columns: Iterable[object]) -> ColumnDetection:
    originals = [str(column) for column in columns]
    normalized = defaultdict(list)
    for column in originals:
        normalized[normalize_header(column)].append(column)

    mapping: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for field_name in CANONICAL_FIELDS:
        candidates: list[str] = []
        for alias in _ALIASES[field_name]:
            candidates.extend(normalized.get(normalize_header(alias), []))
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            mapping[field_name] = candidates[0]
        elif len(candidates) > 1:
            ambiguous[field_name] = candidates
    return ColumnDetection(mapping=mapping, ambiguous=ambiguous)


def validate_mapping(mapping: dict[str, str], columns: Iterable[object]) -> list[str]:
    available = {str(column) for column in columns}
    errors: list[str] = []
    full_name = mapping.get("full_name") in available
    split_name = mapping.get("first_name") in available or mapping.get("last_name") in available
    if not full_name and not split_name:
        errors.append("Map Full name or at least one First/Last name column.")
    selected = [column for column in mapping.values() if column]
    duplicates = sorted({column for column in selected if selected.count(column) > 1})
    if duplicates:
        errors.append(f"A source column cannot map to multiple fields: {', '.join(duplicates)}")
    missing = sorted({column for column in selected if column not in available})
    if missing:
        errors.append(f"Mapped columns are missing from the selected sheet: {', '.join(missing)}")
    return errors


def read_source_sheets(path: Path | str) -> dict[str, pd.DataFrame]:
    source = Path(path)
    data = source.read_bytes()
    return read_source_bytes(data, source.name)


def read_source_bytes(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".xlsx":
        workbook = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=object)
        return {
            str(sheet): frame.dropna(how="all").reset_index(drop=True)
            for sheet, frame in workbook.items()
        }
    if suffix != ".csv":
        raise ValueError(f"Unsupported file type for {filename}; use CSV or XLSX")
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            frame = pd.read_csv(
                io.BytesIO(data),
                dtype=object,
                encoding=encoding,
                sep=None,
                engine="python",
            )
            return {"CSV": frame.dropna(how="all").reset_index(drop=True)}
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            errors.append(str(exc))
    raise ValueError(f"Could not parse {filename}: {'; '.join(errors[-2:])}")


def parse_google_sheet_url(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != "docs.google.com":
        raise ValueError("Google Sheet links must use https://docs.google.com/spreadsheets/d/...")
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ValueError("The Google Sheet link does not contain a spreadsheet ID")
    sheet_id = match.group(1)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    gid = str((query.get("gid") or fragment.get("gid") or ["0"])[0])
    if not gid.isdigit():
        raise ValueError("The Google Sheet gid must be numeric")
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    )
    return sheet_id, gid, export_url


def fetch_public_google_sheet(url: str, *, timeout: int = 20) -> tuple[str, bytes]:
    sheet_id, gid, export_url = parse_google_sheet_url(url)
    request = urllib.request.Request(export_url, headers={"User-Agent": "Speedy-Scraper/3.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except Exception as exc:
        raise ValueError(
            "Could not download the Google Sheet. Confirm that the selected tab is public."
        ) from exc
    if not data.strip():
        raise ValueError("The Google Sheet returned an empty CSV")
    return f"gsheet_{sheet_id}_{gid}.csv", data


def canonicalize_frame(
    frame: pd.DataFrame,
    spec: SourceSpec,
    *,
    default_phone_region: str = "IN",
) -> tuple[list[CanonicalRecord], list[InvalidRow]]:
    errors = validate_mapping(spec.mapping, frame.columns)
    if errors:
        raise ValueError(f"{spec.source_name}: {' '.join(errors)}")
    records: list[CanonicalRecord] = []
    invalid: list[InvalidRow] = []
    for frame_index, row in frame.iterrows():
        row_number = int(frame_index) + 2
        raw = {str(column): _display_value(row.get(column)) for column in frame.columns}
        if not any(raw.values()):
            continue
        values = {
            field_name: _display_value(row.get(column))
            for field_name, column in spec.mapping.items()
            if column
        }
        full_name = values.get("full_name", "")
        first_name = values.get("first_name", "")
        last_name = values.get("last_name", "")
        if not full_name:
            full_name = " ".join(part for part in (first_name, last_name) if part).strip()
        normalized_name = normalize_identity_text(full_name)
        normalized_email = normalize_email(values.get("email", ""))
        normalized_phone = normalize_phone(values.get("phone", ""), default_phone_region)
        normalized_linkedin = normalize_linkedin_url(values.get("linkedin_url", ""))
        normalized_company = normalize_company(values.get("company", ""))
        issues: list[str] = []
        if values.get("email") and not normalized_email:
            issues.append("malformed_email")
        if values.get("phone") and not normalized_phone:
            issues.append("malformed_phone")
        if values.get("linkedin_url") and not normalized_linkedin:
            issues.append("invalid_linkedin_url")
        if not normalized_name:
            invalid.append(
                InvalidRow(
                    source_id=spec.source_id,
                    source_name=spec.source_name,
                    sheet_name=spec.sheet_name,
                    row_number=row_number,
                    reason="missing_identity_name",
                    disposition="REJECTED",
                    raw=raw,
                )
            )
            continue
        if not any(
            (normalized_email, normalized_phone, normalized_linkedin, normalized_company)
        ):
            issues.append("weak_identity_name_only")
        record = CanonicalRecord(
            record_id=f"{spec.source_id}:{row_number}",
            source_id=spec.source_id,
            source_name=spec.source_name,
            sheet_name=spec.sheet_name,
            source_role=spec.role,
            row_number=row_number,
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            company=values.get("company", ""),
            designation=values.get("designation", ""),
            email=values.get("email", ""),
            phone=values.get("phone", ""),
            linkedin_url=values.get("linkedin_url", ""),
            status=values.get("status", ""),
            response=values.get("response", ""),
            notes=values.get("notes", ""),
            owner=values.get("owner", ""),
            contact_date=values.get("contact_date", ""),
            normalized_name=normalized_name,
            normalized_company=normalized_company,
            normalized_email=normalized_email,
            normalized_phone=normalized_phone,
            normalized_linkedin=normalized_linkedin,
            raw=raw,
            issues=issues,
        )
        records.append(record)
        for issue in issues:
            if issue != "weak_identity_name_only":
                invalid.append(
                    InvalidRow(
                        source_id=spec.source_id,
                        source_name=spec.source_name,
                        sheet_name=spec.sheet_name,
                        row_number=row_number,
                        reason=issue,
                        disposition="PROCESSED_WITH_WARNING",
                        raw=raw,
                    )
                )
    return records, invalid


def suggest_status_category(value: object) -> StatusCategory:
    status = normalize_identity_text(value)
    if not status:
        return StatusCategory.UNKNOWN
    if any(term in status for term in ("do not contact", "dnc", "unsubscribe", "blocked")):
        return StatusCategory.DO_NOT_CONTACT
    if any(term in status for term in ("positive", "interested", "confirmed", "accepted")):
        return StatusCategory.POSITIVE_RESPONSE
    if any(term in status for term in ("negative", "not interested", "declined", "rejected")):
        return StatusCategory.NEGATIVE_RESPONSE
    if any(term in status for term in ("no response", "no reply", "unresponsive")):
        return StatusCategory.NO_RESPONSE
    if any(term in status for term in ("safe", "recontact", "reach out again", "available")):
        return StatusCategory.SAFE_OR_RECONTACT_ALLOWED
    if any(term in status for term in ("review", "check", "uncertain")):
        return StatusCategory.REVIEW
    if any(term in status for term in ("contacted", "reached out", "emailed", "sent")):
        return StatusCategory.CONTACTED
    return StatusCategory.UNKNOWN


def run_outreach_match(
    primary_records: list[CanonicalRecord],
    previous_records: list[CanonicalRecord],
    *,
    status_map: dict[str, str] | None = None,
    invalid_rows: list[InvalidRow] | None = None,
    config: dict[str, Any] | None = None,
) -> OutreachRun:
    status_map = status_map or {}
    invalid = list(invalid_rows or [])
    unique_primary, duplicate_rows = _deduplicate_primary(primary_records)
    invalid.extend(duplicate_rows)
    previous_duplicate_count = _append_previous_duplicate_warnings(previous_records, invalid)
    clusters = _cluster_previous(previous_records)
    indexes = _cluster_indexes(clusters)
    matches = [
        _match_primary(record, clusters, indexes, status_map) for record in unique_primary
    ]
    metrics = _metrics(
        unique_primary,
        previous_records,
        matches,
        invalid,
        len(duplicate_rows) + previous_duplicate_count,
        clusters,
    )
    return OutreachRun(
        primary_records=unique_primary,
        previous_records=previous_records,
        matches=matches,
        invalid_rows=invalid,
        metrics=metrics,
        config=dict(config or {}),
    )


def apply_review_overrides(
    run: OutreachRun,
    overrides: dict[str, str] | None,
    status_map: dict[str, str] | None = None,
) -> OutreachRun:
    overrides = overrides or {}
    status_map = status_map or {}
    previous_by_id = {record.record_id: record for record in run.previous_records}
    updated: list[MatchResult] = []
    for result in run.matches:
        try:
            action = ReviewAction(overrides.get(result.primary_id, ReviewAction.UNREVIEWED))
        except ValueError:
            action = ReviewAction.UNREVIEWED
        if action == ReviewAction.CONFIRM_MATCH and result.previous_ids:
            histories = [
                previous_by_id[record_id]
                for record_id in result.previous_ids
                if record_id in previous_by_id
            ]
            decision = _decision_from_history(histories, status_map)
            updated.append(
                replace(
                    result,
                    confirmed=True,
                    decision=decision,
                    explanation=f"Manually confirmed. {result.explanation}",
                )
            )
        elif action in {ReviewAction.REJECT_MATCH, ReviewAction.MARK_SAFE}:
            updated.append(
                replace(
                    result,
                    confirmed=False,
                    decision=OutreachDecision.SAFE_TO_CONTACT,
                    explanation=f"Manual decision: {action.value}",
                )
            )
        elif action == ReviewAction.MARK_DO_NOT_CONTACT:
            updated.append(
                replace(
                    result,
                    confirmed=result.confirmed,
                    decision=OutreachDecision.DO_NOT_CONTACT,
                    explanation="Manual decision: MARK_DO_NOT_CONTACT",
                )
            )
        else:
            updated.append(result)
    revised = replace(run, matches=updated)
    clusters = _cluster_previous(revised.previous_records)
    revised.metrics = _metrics(
        revised.primary_records,
        revised.previous_records,
        revised.matches,
        revised.invalid_rows,
        int(run.metrics.get("duplicates", 0)),
        clusters,
    )
    return revised


def build_outreach_frames(
    run: OutreachRun,
    *,
    overrides: dict[str, str] | None = None,
    status_map: dict[str, str] | None = None,
) -> dict[str, pd.DataFrame]:
    effective = apply_review_overrides(run, overrides, status_map)
    primary_by_id = {record.record_id: record for record in effective.primary_records}
    previous_by_id = {record.record_id: record for record in effective.previous_records}

    fresh_rows: list[dict[str, Any]] = []
    common_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    for result in effective.matches:
        primary = primary_by_id[result.primary_id]
        base = _match_export_row(primary, result)
        if result.decision == OutreachDecision.SAFE_TO_CONTACT:
            fresh_rows.append(base)
        if result.confirmed:
            for previous_id in result.previous_ids:
                previous = previous_by_id.get(previous_id)
                if previous:
                    row = dict(base)
                    row.update(_history_export_row(previous, status_map or {}))
                    common_rows.append(row)
        if result.decision in {
            OutreachDecision.REVIEW_REQUIRED,
            OutreachDecision.POSSIBLE_MATCH,
        }:
            row = dict(base)
            candidates = [
                previous_by_id[record_id]
                for record_id in result.previous_ids
                if record_id in previous_by_id
            ]
            row.update(_review_previous_rows(candidates))
            row["Review Action"] = str(
                (overrides or {}).get(result.primary_id, ReviewAction.UNREVIEWED)
            )
            review_rows.append(row)
        for previous_id in result.previous_ids:
            previous = previous_by_id.get(previous_id)
            if previous:
                row = {
                    "Primary Record ID": primary.record_id,
                    "Primary Name": primary.full_name,
                    "Match Type": result.match_type.value,
                    "Confidence": result.confidence,
                    "Confirmed": result.confirmed,
                }
                row.update(_history_export_row(previous, status_map or {}))
                history_rows.append(row)

    combined_rows = _combined_rows(effective)
    invalid_rows = [_invalid_export_row(row) for row in effective.invalid_rows]
    for result in effective.matches:
        if (
            result.decision == OutreachDecision.REVIEW_REQUIRED
            and not result.confirmed
            and result.previous_ids
        ):
            primary = primary_by_id[result.primary_id]
            invalid_rows.append(
                {
                    "Source": primary.source_name,
                    "Sheet": primary.sheet_name,
                    "Row": primary.row_number,
                    "Reason": "conflicting_or_ambiguous_identity",
                    "Disposition": "REVIEW_REQUIRED",
                    "Duplicate Of": "",
                    "Name": primary.full_name,
                    "Company": primary.company,
                    "Match Explanation": result.explanation,
                }
            )
    config_rows = _config_rows(effective.config, status_map or {})
    return {
        "fresh": pd.DataFrame(fresh_rows),
        "common": pd.DataFrame(common_rows),
        "review": pd.DataFrame(review_rows),
        "combined": pd.DataFrame(combined_rows),
        "invalid": pd.DataFrame(invalid_rows),
        "history": pd.DataFrame(history_rows),
        "config": pd.DataFrame(config_rows),
    }


def write_outreach_exports(
    run: OutreachRun,
    job_dir: Path | str,
    *,
    overrides: dict[str, str] | None = None,
    status_map: dict[str, str] | None = None,
) -> dict[str, str]:
    path = Path(job_dir)
    path.mkdir(parents=True, exist_ok=True)
    frames = build_outreach_frames(run, overrides=overrides, status_map=status_map)
    names = {
        "fresh": "Fresh_Outreach.csv",
        "common": "Common_People.csv",
        "review": "Review_Queue.csv",
        "combined": "Combined_Master.csv",
        "invalid": "Invalid_Rejected_Rows.csv",
        "history": "Interaction_History.csv",
    }
    outputs: dict[str, str] = {}
    for key, filename in names.items():
        target = path / filename
        frames[key].to_csv(target, index=False)
        outputs[f"{key}_csv"] = str(target.resolve())
    workbook = path / "Outreach_Intelligence_Audit.xlsx"
    sheet_names = {
        "fresh": "Fresh Outreach",
        "common": "Common People",
        "review": "Review Queue",
        "combined": "Combined Master",
        "invalid": "Invalid Rows",
        "history": "Interaction History",
        "config": "Run Configuration",
    }
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        for key, sheet_name in sheet_names.items():
            frames[key].to_excel(writer, sheet_name=sheet_name, index=False)
    outputs["xlsx"] = str(workbook.resolve())
    return outputs


def save_review_decisions(job_dir: Path | str, overrides: dict[str, str]) -> None:
    write_json(Path(job_dir) / "review_decisions.json", overrides)


def load_review_decisions(job_dir: Path | str) -> dict[str, str]:
    value = read_json(Path(job_dir) / "review_decisions.json", default={})
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def outreach_run_to_dict(run: OutreachRun) -> dict[str, Any]:
    return asdict(run)


def outreach_run_from_dict(value: dict[str, Any]) -> OutreachRun:
    return OutreachRun(
        primary_records=[_record_from_dict(item) for item in value.get("primary_records", [])],
        previous_records=[_record_from_dict(item) for item in value.get("previous_records", [])],
        matches=[_match_from_dict(item) for item in value.get("matches", [])],
        invalid_rows=[InvalidRow(**item) for item in value.get("invalid_rows", [])],
        metrics={str(key): int(item) for key, item in dict(value.get("metrics") or {}).items()},
        config=dict(value.get("config") or {}),
    )


def _record_from_dict(value: dict[str, Any]) -> CanonicalRecord:
    data = dict(value)
    data["source_role"] = SourceRole(data["source_role"])
    return CanonicalRecord(**data)


def _match_from_dict(value: dict[str, Any]) -> MatchResult:
    data = dict(value)
    data["match_type"] = MatchType(data["match_type"])
    data["decision"] = OutreachDecision(data["decision"])
    return MatchResult(**data)


class _UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _cluster_previous(records: list[CanonicalRecord]) -> dict[str, list[CanonicalRecord]]:
    union = _UnionFind(record.record_id for record in records)
    for attribute in ("normalized_phone", "normalized_email", "normalized_linkedin"):
        seen: dict[str, str] = {}
        for record in records:
            key = getattr(record, attribute)
            if not key:
                continue
            if key in seen:
                union.union(record.record_id, seen[key])
            else:
                seen[key] = record.record_id
    by_name_company: dict[tuple[str, str], list[CanonicalRecord]] = defaultdict(list)
    for record in records:
        if record.normalized_name and record.normalized_company:
            by_name_company[(record.normalized_name, record.normalized_company)].append(record)
    for group in by_name_company.values():
        anchor = group[0]
        for record in group[1:]:
            if not _records_have_strong_conflict(anchor, record):
                union.union(anchor.record_id, record.record_id)
    clusters: dict[str, list[CanonicalRecord]] = defaultdict(list)
    for record in records:
        clusters[union.find(record.record_id)].append(record)
    return dict(clusters)


def _records_have_strong_conflict(left: CanonicalRecord, right: CanonicalRecord) -> bool:
    for attribute in ("normalized_phone", "normalized_email", "normalized_linkedin"):
        left_value = getattr(left, attribute)
        right_value = getattr(right, attribute)
        if left_value and right_value and left_value != right_value:
            return True
    return False


def _cluster_indexes(
    clusters: dict[str, list[CanonicalRecord]],
) -> dict[str, dict[Any, set[str]]]:
    indexes: dict[str, dict[Any, set[str]]] = {
        "phone": defaultdict(set),
        "email": defaultdict(set),
        "linkedin": defaultdict(set),
        "name_company": defaultdict(set),
        "initial": defaultdict(set),
        "surname": defaultdict(set),
    }
    for root, records in clusters.items():
        for record in records:
            if record.normalized_phone:
                indexes["phone"][record.normalized_phone].add(root)
            if record.normalized_email:
                indexes["email"][record.normalized_email].add(root)
            if record.normalized_linkedin:
                indexes["linkedin"][record.normalized_linkedin].add(root)
            if record.normalized_name and record.normalized_company:
                indexes["name_company"][
                    (record.normalized_name, record.normalized_company)
                ].add(root)
            tokens = record.normalized_name.split()
            if tokens:
                indexes["initial"][tokens[0][0]].add(root)
                indexes["surname"][tokens[-1]].add(root)
    return indexes


def _match_primary(
    primary: CanonicalRecord,
    clusters: dict[str, list[CanonicalRecord]],
    indexes: dict[str, dict[Any, set[str]]],
    status_map: dict[str, str],
) -> MatchResult:
    strong = {
        "phone": set(indexes["phone"].get(primary.normalized_phone, set()))
        if primary.normalized_phone
        else set(),
        "email": set(indexes["email"].get(primary.normalized_email, set()))
        if primary.normalized_email
        else set(),
        "linkedin": set(indexes["linkedin"].get(primary.normalized_linkedin, set()))
        if primary.normalized_linkedin
        else set(),
    }
    nonempty = [roots for roots in strong.values() if roots]
    all_roots = set().union(*nonempty) if nonempty else set()
    shared = set.intersection(*nonempty) if nonempty else set()
    if nonempty and (not shared or len(all_roots) > 1):
        previous_ids = _cluster_record_ids(clusters, all_roots)
        return MatchResult(
            primary_id=primary.record_id,
            previous_ids=previous_ids,
            match_type=_strongest_type(strong),
            confidence=70,
            matched_fields=[field for field, roots in strong.items() if roots],
            explanation="Strong identifiers point to different historical people.",
            decision=OutreachDecision.REVIEW_REQUIRED,
            confirmed=False,
        )
    if shared:
        root = next(iter(shared))
        histories = clusters[root]
        match_type, confidence = _strongest_type_and_confidence(strong)
        if _name_conflicts(primary, histories):
            return MatchResult(
                primary_id=primary.record_id,
                previous_ids=[record.record_id for record in histories],
                match_type=match_type,
                confidence=75,
                matched_fields=[field for field, roots in strong.items() if root in roots],
                explanation="A strong identifier matched but the names conflict materially.",
                decision=OutreachDecision.REVIEW_REQUIRED,
                confirmed=False,
            )
        return MatchResult(
            primary_id=primary.record_id,
            previous_ids=[record.record_id for record in histories],
            match_type=match_type,
            confidence=confidence,
            matched_fields=[field for field, roots in strong.items() if root in roots],
            explanation=f"Matched exact normalized {match_type.value.removeprefix('EXACT_').lower()}.",
            decision=_decision_from_history(histories, status_map),
            confirmed=True,
        )

    name_company_roots = set()
    if primary.normalized_name and primary.normalized_company:
        name_company_roots = set(
            indexes["name_company"].get(
                (primary.normalized_name, primary.normalized_company), set()
            )
        )
    if len(name_company_roots) == 1:
        root = next(iter(name_company_roots))
        histories = clusters[root]
        return MatchResult(
            primary_id=primary.record_id,
            previous_ids=[record.record_id for record in histories],
            match_type=MatchType.NAME_COMPANY_MATCH,
            confidence=90,
            matched_fields=["name", "company"],
            explanation="Normalized name and company match exactly.",
            decision=_decision_from_history(histories, status_map),
            confirmed=True,
        )
    if len(name_company_roots) > 1:
        return MatchResult(
            primary_id=primary.record_id,
            previous_ids=_cluster_record_ids(clusters, name_company_roots),
            match_type=MatchType.NAME_COMPANY_MATCH,
            confidence=70,
            matched_fields=["name", "company"],
            explanation="Name and company match more than one historical identity.",
            decision=OutreachDecision.REVIEW_REQUIRED,
            confirmed=False,
        )

    fuzzy_result = _fuzzy_match(primary, clusters, indexes)
    if fuzzy_result:
        return fuzzy_result
    return MatchResult(
        primary_id=primary.record_id,
        previous_ids=[],
        match_type=MatchType.NO_MATCH,
        confidence=0,
        matched_fields=[],
        explanation="No historical identity passed the matching thresholds.",
        decision=OutreachDecision.SAFE_TO_CONTACT,
        confirmed=False,
    )


def _fuzzy_match(
    primary: CanonicalRecord,
    clusters: dict[str, list[CanonicalRecord]],
    indexes: dict[str, dict[Any, set[str]]],
) -> MatchResult | None:
    tokens = primary.normalized_name.split()
    if not tokens:
        return None
    candidate_roots = set(indexes["initial"].get(tokens[0][0], set()))
    candidate_roots.update(indexes["surname"].get(tokens[-1], set()))
    scored: list[tuple[int, int, int, str]] = []
    for root in candidate_roots:
        best_name = 0
        best_company = 0
        for previous in clusters[root]:
            best_name = max(
                best_name,
                round(fuzz.token_sort_ratio(primary.normalized_name, previous.normalized_name)),
            )
            if primary.normalized_company and previous.normalized_company:
                best_company = max(
                    best_company,
                    round(
                        fuzz.token_set_ratio(
                            primary.normalized_company, previous.normalized_company
                        )
                    ),
                )
        if primary.normalized_company and best_name >= 90 and best_company >= 80:
            score = round(best_name * 0.7 + best_company * 0.3)
            scored.append((score, best_name, best_company, root))
        elif not primary.normalized_company and best_name >= 95:
            scored.append((best_name, best_name, 0, root))
    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0]
    tied_roots = [item[3] for item in scored if best[0] - item[0] <= 2]
    explanation = (
        f"Possible fuzzy identity: name {best[1]}%, company {best[2]}%."
        if best[2]
        else f"Possible name-only identity: name {best[1]}%."
    )
    if len(tied_roots) > 1:
        explanation += " Multiple historical identities scored similarly."
    return MatchResult(
        primary_id=primary.record_id,
        previous_ids=_cluster_record_ids(clusters, set(tied_roots)),
        match_type=MatchType.POSSIBLE_MATCH,
        confidence=best[0],
        matched_fields=["name", "company"] if best[2] else ["name"],
        explanation=explanation,
        decision=OutreachDecision.POSSIBLE_MATCH,
        confirmed=False,
    )


def _decision_from_history(
    histories: list[CanonicalRecord], status_map: dict[str, str]
) -> OutreachDecision:
    categories = {_status_category(record.status, status_map) for record in histories}
    if StatusCategory.DO_NOT_CONTACT in categories:
        return OutreachDecision.DO_NOT_CONTACT
    contacted = {
        StatusCategory.CONTACTED,
        StatusCategory.POSITIVE_RESPONSE,
        StatusCategory.NEGATIVE_RESPONSE,
        StatusCategory.NO_RESPONSE,
    }
    if categories & contacted:
        return OutreachDecision.ALREADY_CONTACTED
    if categories and categories <= {StatusCategory.SAFE_OR_RECONTACT_ALLOWED}:
        return OutreachDecision.SAFE_TO_CONTACT
    return OutreachDecision.REVIEW_REQUIRED


def _status_category(value: str, status_map: dict[str, str]) -> StatusCategory:
    mapped = status_map.get(value)
    if mapped is None:
        mapped = status_map.get(normalize_identity_text(value))
    if mapped is None:
        return suggest_status_category(value)
    try:
        return StatusCategory(mapped)
    except ValueError:
        return StatusCategory.UNKNOWN


def _strongest_type(strong: dict[str, set[str]]) -> MatchType:
    return _strongest_type_and_confidence(strong)[0]


def _strongest_type_and_confidence(
    strong: dict[str, set[str]],
) -> tuple[MatchType, int]:
    if strong["phone"]:
        return MatchType.EXACT_PHONE, 100
    if strong["email"]:
        return MatchType.EXACT_EMAIL, 99
    return MatchType.EXACT_LINKEDIN, 98


def _name_conflicts(primary: CanonicalRecord, histories: list[CanonicalRecord]) -> bool:
    if not primary.normalized_name:
        return False
    scores = [
        fuzz.token_sort_ratio(primary.normalized_name, item.normalized_name)
        for item in histories
        if item.normalized_name
    ]
    return bool(scores) and max(scores) < 60


def _cluster_record_ids(
    clusters: dict[str, list[CanonicalRecord]], roots: set[str]
) -> list[str]:
    return [record.record_id for root in sorted(roots) for record in clusters[root]]


def _deduplicate_primary(
    records: list[CanonicalRecord],
) -> tuple[list[CanonicalRecord], list[InvalidRow]]:
    seen: dict[tuple[str, str], str] = {}
    unique: list[CanonicalRecord] = []
    duplicates: list[InvalidRow] = []
    for record in records:
        key = _best_identity_key(record)
        if key and key in seen:
            duplicates.append(
                InvalidRow(
                    source_id=record.source_id,
                    source_name=record.source_name,
                    sheet_name=record.sheet_name,
                    row_number=record.row_number,
                    reason="duplicate_primary_record",
                    disposition="DUPLICATE_SKIPPED",
                    duplicate_of=seen[key],
                    raw=record.raw,
                )
            )
        else:
            if key:
                seen[key] = record.record_id
            unique.append(record)
    return unique, duplicates


def _append_previous_duplicate_warnings(
    records: list[CanonicalRecord], invalid: list[InvalidRow]
) -> int:
    seen_by_source: dict[tuple[str, tuple[str, str]], str] = {}
    duplicate_count = 0
    for record in records:
        key = _best_identity_key(record)
        if not key:
            continue
        scoped_key = (record.source_id, key)
        if scoped_key in seen_by_source:
            duplicate_count += 1
            invalid.append(
                InvalidRow(
                    source_id=record.source_id,
                    source_name=record.source_name,
                    sheet_name=record.sheet_name,
                    row_number=record.row_number,
                    reason="duplicate_previous_record",
                    disposition="PRESERVED_AS_HISTORY",
                    duplicate_of=seen_by_source[scoped_key],
                    raw=record.raw,
                )
            )
        else:
            seen_by_source[scoped_key] = record.record_id
    return duplicate_count


def _best_identity_key(record: CanonicalRecord) -> tuple[str, str] | None:
    for name, value in (
        ("phone", record.normalized_phone),
        ("email", record.normalized_email),
        ("linkedin", record.normalized_linkedin),
    ):
        if value:
            return name, value
    if record.normalized_name and record.normalized_company:
        return "name_company", f"{record.normalized_name}|{record.normalized_company}"
    return None


def _metrics(
    primary: list[CanonicalRecord],
    previous: list[CanonicalRecord],
    matches: list[MatchResult],
    invalid: list[InvalidRow],
    duplicates: int,
    clusters: dict[str, list[CanonicalRecord]],
) -> dict[str, int]:
    confirmed_previous = {
        previous_id
        for result in matches
        if result.confirmed
        for previous_id in result.previous_ids
    }
    return {
        "primary_prospects": len(primary),
        "previous_records": len(previous),
        "unique_people": len(primary)
        + sum(
            1
            for records in clusters.values()
            if not any(record.record_id in confirmed_previous for record in records)
        ),
        "common_people": sum(result.confirmed for result in matches),
        "safe_to_contact": sum(
            result.decision == OutreachDecision.SAFE_TO_CONTACT for result in matches
        ),
        "already_contacted": sum(
            result.decision == OutreachDecision.ALREADY_CONTACTED for result in matches
        ),
        "do_not_contact": sum(
            result.decision == OutreachDecision.DO_NOT_CONTACT for result in matches
        ),
        "possible_matches": sum(
            result.decision == OutreachDecision.POSSIBLE_MATCH for result in matches
        ),
        "review_required": sum(
            result.decision == OutreachDecision.REVIEW_REQUIRED for result in matches
        ),
        "duplicates": duplicates,
        "invalid_rows": sum(row.disposition == "REJECTED" for row in invalid),
    }


def _match_export_row(primary: CanonicalRecord, result: MatchResult) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Primary Record ID": primary.record_id,
        "Name": primary.full_name,
        "Designation": primary.designation,
        "Company": primary.company,
        "Email": primary.email,
        "Phone": primary.phone,
        "LinkedIn URL": primary.linkedin_url,
        "Final Decision": result.decision.value,
        "Match Type": result.match_type.value,
        "Confidence": result.confidence,
        "Confirmed Match": result.confirmed,
        "Matched Fields": ", ".join(result.matched_fields),
        "Match Explanation": result.explanation,
        "Interaction Count": len(result.previous_ids),
        "Primary Source": primary.source_name,
        "Primary Sheet": primary.sheet_name,
        "Primary Row": primary.row_number,
    }
    for key, value in primary.raw.items():
        row[f"Primary Original - {key}"] = value
    return row


def _history_export_row(
    previous: CanonicalRecord, status_map: dict[str, str]
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Previous Record ID": previous.record_id,
        "Previous Name": previous.full_name,
        "Previous Company": previous.company,
        "Previous Designation": previous.designation,
        "Previous Email": previous.email,
        "Previous Phone": previous.phone,
        "Previous LinkedIn URL": previous.linkedin_url,
        "Previous Status": previous.status,
        "Previous Canonical Status": _status_category(previous.status, status_map).value,
        "Previous Response": previous.response,
        "Previous Notes": previous.notes,
        "Previous Owner": previous.owner,
        "Previous Contact Date": previous.contact_date,
        "Previous Source": previous.source_name,
        "Previous Sheet": previous.sheet_name,
        "Previous Row": previous.row_number,
    }
    for key, value in previous.raw.items():
        row[f"Previous Original - {key}"] = value
    return row


def _review_previous_rows(previous: list[CanonicalRecord]) -> dict[str, Any]:
    if not previous:
        return {
            "Previous Candidate IDs": "",
            "Previous Name": "",
            "Previous Company": "",
            "Previous Status": "",
            "Previous Response": "",
            "Previous Source": "",
            "Previous Row": "",
        }
    return {
        "Previous Candidate IDs": " | ".join(record.record_id for record in previous),
        "Previous Name": " | ".join(record.full_name for record in previous),
        "Previous Company": " | ".join(record.company for record in previous),
        "Previous Status": " | ".join(record.status for record in previous),
        "Previous Response": " | ".join(record.response for record in previous),
        "Previous Source": " | ".join(record.source_name for record in previous),
        "Previous Row": " | ".join(str(record.row_number) for record in previous),
    }


def _combined_rows(run: OutreachRun) -> list[dict[str, Any]]:
    primary_by_id = {record.record_id: record for record in run.primary_records}
    previous_by_id = {record.record_id: record for record in run.previous_records}
    matched_previous: set[str] = set()
    rows: list[dict[str, Any]] = []
    for result in run.matches:
        primary = primary_by_id[result.primary_id]
        if result.confirmed:
            matched_previous.update(result.previous_ids)
        row = _match_export_row(primary, result)
        row["Membership"] = "BOTH" if result.confirmed else "PRIMARY_ONLY"
        histories = [previous_by_id[item] for item in result.previous_ids if item in previous_by_id]
        row["Previous Status History"] = " | ".join(
            item.status for item in histories if item.status
        )
        row["Previous Response History"] = " | ".join(
            item.response for item in histories if item.response
        )
        rows.append(row)
    for cluster in _cluster_previous(run.previous_records).values():
        if any(record.record_id in matched_previous for record in cluster):
            continue
        representative = cluster[0]
        row = {
            "Membership": "PREVIOUS_ONLY",
            "Previous Record ID": representative.record_id,
            "Name": representative.full_name,
            "Designation": representative.designation,
            "Company": representative.company,
            "Email": representative.email,
            "Phone": representative.phone,
            "LinkedIn URL": representative.linkedin_url,
            "Previous Status History": " | ".join(
                item.status for item in cluster if item.status
            ),
            "Previous Response History": " | ".join(
                item.response for item in cluster if item.response
            ),
            "Interaction Count": len(cluster),
            "Previous Source": representative.source_name,
            "Previous Sheet": representative.sheet_name,
            "Previous Row": representative.row_number,
        }
        for key, value in representative.raw.items():
            row[f"Previous Original - {key}"] = value
        rows.append(row)
    return rows


def _invalid_export_row(row: InvalidRow) -> dict[str, Any]:
    result: dict[str, Any] = {
        "Source": row.source_name,
        "Sheet": row.sheet_name,
        "Row": row.row_number,
        "Reason": row.reason,
        "Disposition": row.disposition,
        "Duplicate Of": row.duplicate_of,
    }
    result.update({f"Original - {key}": value for key, value in row.raw.items()})
    return result


def _config_rows(config: dict[str, Any], status_map: dict[str, str]) -> list[dict[str, str]]:
    rows = [
        {"Setting": str(key), "Value": _display_value(value)}
        for key, value in config.items()
        if key not in {"primary", "previous"}
    ]
    rows.extend(
        {"Setting": f"Status mapping: {key or '<blank>'}", "Value": value}
        for key, value in status_map.items()
    )
    return rows


def _display_value(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, (dict, list, tuple, set)):
        return str(value)
    return " ".join(str(value).strip().split())
