"""Data-driven role and location expansion shared by query planning and validation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from speedy_scraper.text import clean_spaces, normalize_text, term_in_text, unique_terms

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
ROLE_TAXONOMY_PATH = CONFIG_DIR / "role_taxonomy.yaml"
LOCATION_TAXONOMY_PATH = CONFIG_DIR / "location_taxonomy.yaml"

_ROLE_INPUT_ALIASES = {
    "director talent accquisition": "Director Talent Acquisition",
    "head of talent acquistion": "Head of Talent Acquisition",
    "ta heads": "Head of Talent Acquisition",
    "ta leaders": "Talent Acquisition Leader",
    "talent acquistion head": "Head of Talent Acquisition",
}
_PEOPLE_FUNCTIONS = {"human_resources", "talent_acquisition"}


@dataclass(frozen=True)
class RoleDefinition:
    key: str
    synonyms: tuple[str, ...]
    function: str
    seniority: str
    query_group: str

    @property
    def terms(self) -> list[str]:
        return unique_terms([self.key, *self.synonyms])


@dataclass(frozen=True)
class LocationDefinition:
    key: str
    variants: tuple[str, ...]

    @property
    def terms(self) -> list[str]:
        return unique_terms([self.key, *self.variants])


@lru_cache(maxsize=4)
def load_role_taxonomy(path: str | Path = ROLE_TAXONOMY_PATH) -> dict[str, RoleDefinition]:
    raw = _load_mapping(Path(path), "roles")
    definitions: dict[str, RoleDefinition] = {}
    for key, value in raw.items():
        item = value if isinstance(value, dict) else {}
        definitions[str(key)] = RoleDefinition(
            key=str(key),
            synonyms=tuple(_strings(item.get("synonyms"))),
            function=clean_spaces(str(item.get("function") or normalize_text(str(key)))),
            seniority=clean_spaces(str(item.get("seniority") or "unspecified")),
            query_group=clean_spaces(str(item.get("query_group") or normalize_text(str(key)))),
        )
    return definitions


@lru_cache(maxsize=4)
def load_location_taxonomy(
    path: str | Path = LOCATION_TAXONOMY_PATH,
) -> dict[str, LocationDefinition]:
    raw = _load_mapping(Path(path), "locations")
    definitions: dict[str, LocationDefinition] = {}
    for key, value in raw.items():
        item = value if isinstance(value, dict) else {}
        definitions[str(key)] = LocationDefinition(
            key=str(key),
            variants=tuple(_strings(item.get("variants"))),
        )
    return definitions


def resolve_role(value: str) -> RoleDefinition:
    requested = normalize_text(_ROLE_INPUT_ALIASES.get(normalize_text(value), value))
    definitions = load_role_taxonomy()
    for definition in definitions.values():
        if requested == normalize_text(definition.key):
            return definition
    for definition in definitions.values():
        if requested in {normalize_text(term) for term in definition.terms}:
            return definition
    cleaned = clean_spaces(value)
    return RoleDefinition(
        key=cleaned,
        synonyms=(),
        function=normalize_text(cleaned),
        seniority="unspecified",
        query_group=normalize_text(cleaned),
    )


def contextualize_roles(roles: list[str]) -> list[str]:
    """Resolve ambiguous acronyms and common input mistakes before planning/filtering."""
    selected = unique_terms(roles)
    people_context = any(
        normalize_text(role) != "cpo" and resolve_role(role).function in _PEOPLE_FUNCTIONS
        for role in selected
    )
    normalized: list[str] = []
    for role in selected:
        key = normalize_text(role)
        if key == "cpo" and people_context:
            normalized.append("Chief People Officer")
        else:
            corrected = _ROLE_INPUT_ALIASES.get(key, role)
            normalized.append(resolve_role(corrected).key)
    return unique_terms(normalized)


def resolve_location(value: str) -> LocationDefinition:
    requested = normalize_text(value)
    definitions = load_location_taxonomy()
    for definition in definitions.values():
        if requested == normalize_text(definition.key):
            return definition
    for definition in definitions.values():
        if requested in {normalize_text(term) for term in definition.terms}:
            return definition
    cleaned = clean_spaces(value)
    return LocationDefinition(key=cleaned, variants=(cleaned,))


def role_query_groups(roles: list[str], *, max_terms: int = 4) -> list[list[str]]:
    grouped: dict[str, list[RoleDefinition]] = {}
    order: list[str] = []
    for role in unique_terms(roles):
        definition = resolve_role(role)
        if definition.query_group not in grouped:
            grouped[definition.query_group] = []
            order.append(definition.query_group)
        if definition not in grouped[definition.query_group]:
            grouped[definition.query_group].append(definition)
    result: list[list[str]] = []
    for group in order:
        primary: list[str] = []
        alternates: list[str] = []
        for definition in grouped[group]:
            primary.extend(definition.terms[:2])
            alternates.extend(definition.terms[2:])
        for terms in (unique_terms(primary), unique_terms(alternates)):
            result.extend(
                terms[index : index + max_terms]
                for index in range(0, len(terms), max_terms)
            )
    return [item for item in result if item]


def location_query_groups(locations: list[str], *, max_terms: int = 3) -> list[list[str]]:
    groups: list[list[str]] = []
    seen: set[str] = set()
    for location in unique_terms(locations):
        definition = resolve_location(location)
        key = normalize_text(definition.key)
        if key in seen:
            continue
        seen.add(key)
        groups.append(definition.terms[:max_terms])
    return groups


def canonical_location_from_text(text: str, requested_locations: list[str]) -> str:
    for requested in unique_terms(requested_locations):
        definition = resolve_location(requested)
        if any(term_in_text(text, variant) for variant in definition.terms):
            return definition.key
    return ""


def role_definition_matches(designation: str, requested_role: str) -> int:
    requested = resolve_role(requested_role)
    requested_key = normalize_text(requested_role)
    designation_key = normalize_text(designation)
    is_unclassified_acronym = (
        requested.seniority == "unspecified"
        and " " not in requested_key
        and len(requested_key) <= 3
    )
    has_role_context = any(
        term_in_text(designation, marker)
        for marker in (
            "chief",
            "director",
            "head",
            "lead",
            "leader",
            "manager",
            "president",
            "vice president",
            "vp",
        )
    )
    exact_term = any(term_in_text(designation, term) for term in requested.terms)
    if exact_term and (
        not is_unclassified_acronym or designation_key == requested_key or has_role_context
    ):
        return 3

    candidate_definitions = [
        definition
        for definition in load_role_taxonomy().values()
        if any(term_in_text(designation, term) for term in definition.terms)
    ]
    for candidate in candidate_definitions:
        if (
            requested.seniority != "unspecified"
            and candidate.seniority == requested.seniority
            and (
                candidate.function == requested.function
                or requested.function in {"generic_director", "generic_vp"}
            )
        ):
            requested_is_senior_director = (
                requested.function == "generic_director"
                and "senior" in requested_key
            )
            candidate_is_senior_director = (
                "senior" in designation_key
                or designation_key.startswith("svp ")
            )
            if requested_is_senior_director and not candidate_is_senior_director:
                continue
            return 2
    return 0


def _load_mapping(path: Path, section: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict) or not isinstance(value.get(section), dict):
        raise ValueError(f"{path.name} must contain a '{section}' mapping")
    return dict(value[section])


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [clean_spaces(value)] if clean_spaces(value) else []
    return [clean_spaces(str(item)) for item in value if clean_spaces(str(item))]
