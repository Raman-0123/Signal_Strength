"""CSV and multi-sheet Excel exclusion import parsing."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from core.utils import normalize_linkedin_url, person_identity_key


def parse_dedup_content(name: str, content: bytes) -> tuple[set[tuple[str, str]], int]:
    if name.lower().endswith(".csv"):
        frames: dict[str, pd.DataFrame] = {name: pd.read_csv(io.BytesIO(content))}
    else:
        value: Any = pd.read_excel(io.BytesIO(content), sheet_name=None)
        frames = value if isinstance(value, dict) else {name: value}
    keys: set[tuple[str, str]] = set()
    for frame in frames.values():
        normalized = {
            "".join(ch for ch in str(column).lower() if ch.isalnum()): column
            for column in frame.columns
        }
        url_col = next(
            (value for key, value in normalized.items() if "linkedin" in key or "linkdin" in key),
            None,
        )
        name_col = next(
            (normalized[key] for key in ("name", "fullname", "contactname", "personname") if key in normalized),
            None,
        )
        company_col = next(
            (normalized[key] for key in ("company", "companyname", "organisation", "organization", "employer") if key in normalized),
            None,
        )
        if url_col:
            for value in frame[url_col].dropna():
                if url := normalize_linkedin_url(value):
                    keys.add(("url", url))
        if name_col and company_col:
            for person, company in zip(frame[name_col], frame[company_col]):
                if identity := person_identity_key(person, company):
                    keys.add(("identity", identity))
    return keys, len(frames)

