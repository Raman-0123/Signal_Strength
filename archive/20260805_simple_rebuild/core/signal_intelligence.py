"""Evidence-based signal and business-model classification.

Search-result snippets are noisy: they can mention another person, an audio
speaker, or a company award and still look like a strong POC signal.  This
module keeps acceptance deterministic while requiring contextual evidence.
"""

from __future__ import annotations

import re
from typing import Iterable

from core.utils import normalize_text

EVENT_CONTEXT = (
    "conference", "summit", "event", "forum", "webinar", "panel", "keynote",
    "roundtable", "fireside", "tedx", "conclave", "congress", "symposium",
)

SIGNAL_RULES = (
    {
        "name": "Past Speaker",
        "type": "Person",
        "score": 95,
        "aliases": (
            "speaker", "keynote speaker", "keynote", "panelist",
            "conference speaker", "summit speaker", "tedx", "tedxtalk",
            "panel discussion",
        ),
        "patterns": (
            r"\bkeynote speaker\b", r"\bconference speaker\b",
            r"\bsummit speaker\b", r"\bfeatured speaker\b",
            r"\bguest speaker\b", r"\bpanel(?:ist| speaker)\b",
            r"\bspoke at\b", r"\bspeaking at\b", r"\btedx(?:talk| speaker)?\b",
            r"\bfireside (?:chat|speaker)\b",
            r"\b(?:speakers|panelists) (?:include|included|are|were|featuring)\b",
        ),
    },
    {
        "name": "Roundtable Participant",
        "type": "Person",
        "score": 92,
        "aliases": (
            "roundtable", "cxo roundtable", "leadership summit",
            "executive roundtable", "boardroom", "cxo forum",
            "leadership forum", "c-suite roundtable", "gcc roundtable",
            "global capability center roundtable",
            "global capability centre roundtable", "gcc leadership forum",
            "gcc summit", "gcc conclave",
        ),
        "patterns": (
            r"\bcxo roundtable\b", r"\bc suite roundtable\b",
            r"\bgcc (?:roundtable|leadership forum|summit|conclave)\b",
            r"\bglobal capability cent(?:er|re) roundtable\b",
            r"\bexecutive roundtable\b", r"\bleadership roundtable\b",
            r"\broundtable (?:speaker|panelist|participant|attendee|host|moderator)\b",
            r"\b(?:joined|attended|participated in|spoke at|hosted|moderated)(?: the| an| a)? roundtable\b",
            r"\bcxo forum (?:speaker|panelist|participant|member)\b",
            r"\broundtable (?:with|featuring|included)\b",
        ),
    },
    {
        "name": "Award Recipient",
        "type": "Person",
        "score": 88,
        "aliases": (
            "award", "award winner", "40 under 40", "30 under 30", "forbes",
            "et 40 under 40", "economic times", "business today", "cio100",
            "cmo asia", "recognised", "felicitated",
        ),
        "patterns": (
            r"\baward winner\b", r"\baward recipient\b", r"\bwon (?:the |an? )?[^.]{0,45}\baward\b",
            r"\b(?:awarded|honou?red|felicitated|recognised|recognized) (?:as|with|for|by)\b",
            r"\b(?:40 under 40|30 under 30|cio ?100)\b",
            r"\bforbes (?:india )?(?:list|honou?ree|30 under 30|40 under 40)\b",
        ),
    },
    {
        "name": "Thought Leader",
        "type": "Person",
        "score": 82,
        "aliases": (
            "author", "published", "book author", "linkedin article",
            "thought leader", "columnist", "contributor", "forbes contributor",
            "harvard business review",
        ),
        "patterns": (
            r"\bpublished author\b", r"\bbook author\b", r"\bauthor of\b",
            r"\bthought leader\b", r"\bcolumnist (?:at|for|with)\b",
            r"\bcontributor (?:at|to|for)\b", r"\bforbes contributor\b",
            r"\b(?:writes|wrote) for harvard business review\b",
        ),
    },
    {
        "name": "Board / Advisor",
        "type": "Person",
        "score": 78,
        "aliases": (
            "advisory board", "board member", "mentor", "investor",
            "angel investor", "advisor", "independent director",
            "board of directors",
        ),
        "patterns": (
            r"\badvisory board member\b", r"\bboard member (?:at|of|for)\b",
            r"\bindependent director (?:at|of|for)\b", r"\bangel investor\b",
            r"\badvisor (?:at|to|for)\b", r"\bmentor (?:at|to|for)\b",
            r"\bmember of the board\b",
        ),
    },
    {
        "name": "Association Leader",
        "type": "Person",
        "score": 75,
        "aliases": (
            "nasscom", "cii", "ficci", "tie", "ypo",
            "eo entrepreneurs organization", "industry association",
            "chapter president", "co-chair",
        ),
        "patterns": (
            r"\b(?:chair|co chair|president|vice president|council member|executive member) (?:at|of|for) (?:nasscom|cii|ficci|tie|ypo)\b",
            r"\b(?:nasscom|cii|ficci|tie|ypo) (?:chair|co chair|president|council member|executive member)\b",
            r"\bchapter president\b", r"\bindustry association (?:chair|president|leader)\b",
        ),
    },
    {
        "name": "Company Growth",
        "type": "Company",
        "score": 65,
        "aliases": (
            "series a", "series b", "series c", "ipo", "recently funded",
            "unicorn", "expansion", "new market", "global expansion", "hiring",
        ),
        "patterns": (
            r"\braised (?:a )?series [abc]\b", r"\bseries [abc] (?:funding|round)\b",
            r"\brecently funded\b", r"\b(?:filed for|upcoming|preparing for) (?:an? )?ipo\b",
            r"\breached unicorn status\b", r"\bexpanding (?:into|across|to) (?:a )?new market\b",
            r"\bglobal expansion\b", r"\bhiring (?:across|for|more than|over)\b",
        ),
    },
)


_NEGATIVE_SIGNAL_PATTERNS = (
    r"\b(?:bluetooth|smart|audio|wireless|stereo|loud) speaker\b",
    r"\bspeaker (?:system|phone|volume|driver|repair|manufacturer)\b",
    r"\b(?:looking|searching|hiring|call) for speakers?\b",
    r"\bspeakers? wanted\b",
    r"\baward winning (?:product|platform|software|company|solution|campaign)\b",
)


def _selected_rule_names(selected_signals: Iterable[str] | None) -> set[str]:
    selected = {normalize_text(item) for item in selected_signals or [] if item}
    if not selected:
        return set()
    names = set()
    for rule in SIGNAL_RULES:
        aliases = {normalize_text(item) for item in rule["aliases"]}
        if selected & aliases or any(
            len(alias) >= 5 and (alias in term or term in alias)
            for alias in aliases for term in selected
        ):
            names.add(rule["name"])
    return names


def _person_tokens(person_name: str) -> list[str]:
    ignored = {"unknown", "linkedin", "profile", "dr", "mr", "mrs", "ms"}
    return [
        token for token in normalize_text(person_name).split()
        if len(token) >= 3 and token not in ignored
    ]


def _is_attributed(source: str, text: str, start: int, person_name: str, signal_type: str) -> tuple[bool, str]:
    if source == "title":
        return True, "title"

    # Search snippets normally start with the profile bio/current-position text.
    # Later mentions are often post text about somebody else.
    if start <= 180:
        return True, "profile snippet"

    if signal_type == "Company" and start <= 260:
        return True, "company snippet"

    window = text[max(0, start - 100): start + 130]
    person_tokens = _person_tokens(person_name)
    if person_tokens and any(token in window for token in person_tokens):
        return True, "named snippet"
    return False, "unattributed mention"


def _evidence_window(text: str, start: int, end: int) -> str:
    left = max(0, start - 45)
    right = min(len(text), end + 55)
    evidence = re.sub(r"\s+", " ", text[left:right]).strip(" -|.,")
    return evidence[:180]


def assess_signal(
    title: str,
    body: str,
    person_name: str = "",
    selected_signals: Iterable[str] | None = None,
) -> dict:
    """Return the strongest contextual, attributable signal in a result.

    ``selected_match`` becomes mandatory in the harvesting pipeline when the
    user selected an event/intent signal category.
    """
    title_norm = normalize_text(title)
    body_norm = normalize_text(body)
    selection_requested = any(item for item in selected_signals or [] if item)
    selected_rules = _selected_rule_names(selected_signals)
    candidates = []
    rejected = []

    for rule in SIGNAL_RULES:
        for source, text in (("title", title_norm), ("body", body_norm)):
            if not text:
                continue
            for pattern in rule["patterns"]:
                for match in re.finditer(pattern, text):
                    context = text[max(0, match.start() - 55): match.end() + 65]
                    if any(re.search(negative, context) for negative in _NEGATIVE_SIGNAL_PATTERNS):
                        rejected.append(_evidence_window(text, match.start(), match.end()))
                        continue
                    attributed, attribution = _is_attributed(
                        source, text, match.start(), person_name, rule["type"]
                    )
                    if not attributed:
                        rejected.append(_evidence_window(text, match.start(), match.end()))
                        continue
                    confidence = "High" if attribution in {"title", "named snippet"} else "Medium"
                    candidates.append({
                        "name": rule["name"],
                        "type": rule["type"],
                        "score": rule["score"],
                        "confidence": confidence,
                        "attribution": attribution,
                        "evidence": _evidence_window(text, match.start(), match.end()),
                        "selected": not selection_requested or rule["name"] in selected_rules,
                    })

    # Generic "speaker" is accepted only in actual event context.
    for source, text in (("title", title_norm), ("body", body_norm)):
        for match in re.finditer(r"\bspeaker\b", text):
            context = text[max(0, match.start() - 70): match.end() + 80]
            if not any(re.search(rf"\b{re.escape(noun)}\b", context) for noun in EVENT_CONTEXT):
                continue
            if any(re.search(negative, context) for negative in _NEGATIVE_SIGNAL_PATTERNS):
                continue
            attributed, attribution = _is_attributed(source, text, match.start(), person_name, "Person")
            if attributed:
                candidates.append({
                    "name": "Past Speaker",
                    "type": "Person",
                    "score": 90,
                    "confidence": "High" if attribution in {"title", "named snippet"} else "Medium",
                    "attribution": attribution,
                    "evidence": _evidence_window(text, match.start(), match.end()),
                    "selected": not selection_requested or "Past Speaker" in selected_rules,
                })

    eligible = [item for item in candidates if item["selected"]]
    strongest = max(eligible or candidates, key=lambda item: item["score"], default=None)
    if strongest is None:
        return {
            "name": "No Verified Signal",
            "type": "None",
            "score": 35,
            "confidence": "None",
            "evidence": "",
            "attribution": "none",
            "selected_match": not selection_requested,
            "rejected_mentions": rejected,
        }

    return {
        **strongest,
        "selected_match": bool(eligible) if selection_requested else True,
        "rejected_mentions": rejected,
    }


BUSINESS_MODEL_RULES = {
    "B2B": (
        (3, r"\bb2b\b"), (3, r"\bbusiness to business\b"),
        (3, r"\benterprise (?:software|saas|platform|solution|customers?|clients?)\b"),
        (3, r"\b(?:business|corporate) (?:customers?|clients?)\b"),
        (2, r"\b(?:sells|selling|provides|serves) (?:to )?businesses\b"),
        (2, r"\benterprise sales\b"), (2, r"\bb2b saas\b"),
        (1, r"\bsaas\b"), (1, r"\benterprise\b"),
        (1, r"\bprocurement\b"), (1, r"\bchannel partners?\b"),
    ),
    "B2C": (
        (3, r"\bb2c\b"), (3, r"\bbusiness to consumer\b"),
        (3, r"\bdirect to consumer\b"), (3, r"\bd2c\b"),
        (3, r"\bconsumer (?:app|brand|platform|product|customers?)\b"),
        (2, r"\bretail customers?\b"), (2, r"\bconsumer internet\b"),
        (1, r"\bconsumers?\b"), (1, r"\be commerce\b"),
        (1, r"\bmarketplace\b"), (1, r"\bretail\b"),
    ),
}


def normalize_business_model(value: str | None) -> str:
    normalized = normalize_text(value)
    if not normalized or normalized in {"any", "all", "no filter"}:
        return "Any"
    if "hybrid" in normalized or "both" in normalized:
        return "Hybrid"
    if "b2b" in normalized or "business to business" in normalized:
        return "B2B"
    if "b2c" in normalized or "business to consumer" in normalized or "d2c" in normalized:
        return "B2C"
    return "Any"


def assess_business_model(title: str, body: str, desired_model: str = "Any") -> dict:
    text = normalize_text(f"{title} {body}")
    scores = {"B2B": 0, "B2C": 0}
    evidence = {"B2B": [], "B2C": []}
    for model, rules in BUSINESS_MODEL_RULES.items():
        for weight, pattern in rules:
            match = re.search(pattern, text)
            if match:
                scores[model] += weight
                evidence[model].append(match.group(0))

    if scores["B2B"] >= 2 and scores["B2C"] >= 2:
        detected = "Hybrid"
    elif scores["B2B"] >= 2:
        detected = "B2B"
    elif scores["B2C"] >= 2:
        detected = "B2C"
    else:
        detected = "Unknown"

    desired = normalize_business_model(desired_model)
    if desired == "Any":
        matched = True
    elif desired == "Hybrid":
        matched = detected == "Hybrid"
    elif desired == "B2B":
        matched = detected in {"B2B", "Hybrid"}
    else:
        matched = detected in {"B2C", "Hybrid"}

    strongest_score = max(scores.values())
    confidence = "High" if strongest_score >= 5 else "Medium" if strongest_score >= 2 else "None"
    relevant_evidence = evidence[desired] if desired in evidence else evidence.get(detected, [])
    return {
        "detected": detected,
        "desired": desired,
        "matched": matched,
        "confidence": confidence,
        "evidence": ", ".join(dict.fromkeys(relevant_evidence)),
        "b2b_score": scores["B2B"],
        "b2c_score": scores["B2C"],
    }


def business_model_query_clause(value: str | None) -> str:
    model = normalize_business_model(value)
    if model == "B2B":
        return '("B2B" OR "business customers" OR "enterprise clients" OR "enterprise software" OR "B2B SaaS")'
    if model == "B2C":
        return '("B2C" OR "D2C" OR "consumer brand" OR "consumer app" OR "retail customers")'
    if model == "Hybrid":
        return '("B2B" OR "enterprise clients") ("B2C" OR "D2C" OR "consumer brand")'
    return ""


GCC_TERMS = (
    "GCC", "Global Capability Center", "Global Capability Centre",
    "Global In-house Center", "Global In-house Centre", "GIC",
    "Captive Center", "Captive Centre", "India Capability Center",
    "India Capability Centre",
)


def gcc_query_clause(enabled: bool = False) -> str:
    """Search clause for Global Capability Centres, kept separate from events."""
    if not enabled:
        return ""
    return (
        '("GCC" OR "Global Capability Center" OR "Global Capability Centre" '
        'OR "Global In-house Center" OR "Global In-house Centre" '
        'OR "Captive Center" OR "Captive Centre")'
    )


def assess_gcc(title: str, body: str, enabled: bool = False) -> dict:
    """Classify visible GCC evidence without rejecting sparse search snippets."""
    text = f"{title} {body}"
    matched_terms = [term for term in GCC_TERMS if re.search(
        rf"(?<![a-z0-9]){re.escape(normalize_text(term))}(?![a-z0-9])",
        normalize_text(text),
    )]
    return {
        "requested": bool(enabled),
        "matched": bool(matched_terms),
        "evidence": ", ".join(dict.fromkeys(matched_terms)),
        "confidence": "High" if matched_terms else "None",
    }
