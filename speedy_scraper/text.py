from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str | None) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def clean_spaces(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def quote_term(term: str) -> str:
    term = clean_spaces(term)
    if not term:
        return ""
    return f'"{term}"' if " " in term else term


def or_group(terms: list[str]) -> str:
    cleaned = unique_terms(terms)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return quote_term(cleaned[0])
    return "(" + " OR ".join(quote_term(term) for term in cleaned) + ")"


def unique_terms(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        cleaned = clean_spaces(str(value))
        key = normalize_text(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def term_in_text(text: str, term: str) -> bool:
    haystack = normalize_text(text)
    needle = normalize_text(term)
    if not haystack or not needle:
        return False
    if " " in needle:
        return needle in haystack or needle.replace(" ", "") in haystack.replace(" ", "")
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))


def any_term_in_text(text: str, terms: list[str]) -> bool:
    return any(term_in_text(text, term) for term in terms)

