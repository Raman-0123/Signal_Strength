from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from speedy_scraper.models import RejectedCandidate, ScrapeConfig, ScrapeResult, VerifiedLead

LEAD_COLUMNS = [
    "Name",
    "Designation",
    "Company",
    "Location",
    "LinkedIn ID",
    "LinkedIn URL",
    "Source",
    "Confidence",
    "Evidence",
]

REJECTION_COLUMNS = [
    "Name",
    "Designation",
    "Company",
    "LinkedIn URL",
    "Reason",
    "Source",
    "Evidence",
]


def leads_frame(leads: list[VerifiedLead]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Name": lead.name,
                "Designation": lead.designation,
                "Company": lead.company,
                "Location": lead.location,
                "LinkedIn ID": lead.linkedin_id,
                "LinkedIn URL": lead.linkedin_url,
                "Source": lead.source,
                "Confidence": lead.confidence,
                "Evidence": lead.evidence,
            }
            for lead in leads
        ],
        columns=LEAD_COLUMNS,
    )


def rejections_frame(rejections: list[RejectedCandidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Name": item.name,
                "Designation": item.designation,
                "Company": item.company,
                "LinkedIn URL": item.linkedin_url,
                "Reason": item.reason,
                "Source": item.source,
                "Evidence": item.evidence,
            }
            for item in rejections
        ],
        columns=REJECTION_COLUMNS,
    )


def write_result(
    result: ScrapeResult,
    output: Path | str,
    *,
    config: ScrapeConfig | None = None,
) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        leads_frame(result.leads).to_csv(path, index=False)
        return path
    if path.suffix.lower() not in {".xlsx", ".xls"}:
        path = path.with_suffix(".xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        leads_frame(result.leads).to_excel(writer, sheet_name="Verified Leads", index=False)
        rejections_frame(result.rejections).to_excel(writer, sheet_name="Rejected Candidates", index=False)
        pd.DataFrame([result.metrics]).to_excel(writer, sheet_name="Metrics", index=False)
        pd.DataFrame({"Query": result.queries}).to_excel(writer, sheet_name="Queries", index=False)
        if config is not None:
            rows = []
            for key, value in asdict(config).items():
                if isinstance(value, list):
                    value = "\n".join(str(item) for item in value)
                rows.append({"Parameter": key, "Committed Value": value})
            pd.DataFrame(rows).to_excel(writer, sheet_name="Filter Contract", index=False)
        if result.source_errors:
            pd.DataFrame({"Source Error": result.source_errors}).to_excel(
                writer,
                sheet_name="Source Errors",
                index=False,
            )
    return path
