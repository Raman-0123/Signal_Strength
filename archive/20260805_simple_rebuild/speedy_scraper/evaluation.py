"""Pure candidate qualification and canonical lead construction."""

from __future__ import annotations

from core.signal_intelligence import assess_business_model, assess_gcc, assess_signal
from core.utils import (
    any_term_matches,
    check_location_in_snippet,
    check_role_in_title,
    combined_text,
    extract_profile_location,
    get_detailed_match_summary,
    get_parameter_matches,
    normalize_text,
)
from speedy_scraper.domain import CandidateEvaluation, LeadRecord, ScoringConfig


class CandidateEvaluator:
    def __init__(self, scoring: ScoringConfig | None = None):
        self.scoring = scoring or ScoringConfig()

    def evaluate(
        self,
        *,
        title: str,
        body: str,
        href: str,
        name: str,
        designation: str,
        company: str,
        locations: list[str],
        roles: list[str],
        industries: list[str],
        signals: list[str],
        custom_terms: list[str],
        organization_terms: list[str],
        business_model: str = "Any",
        gcc_only: bool = False,
        company_evidence: str = "",
        person_evidence: str = "",
        strict_stage: bool = True,
    ) -> CandidateEvaluation:
        profile_body = str(body or "")[:840]
        profile_text = combined_text(title, profile_body)
        company_context = combined_text(company, company_evidence)
        signal = assess_signal(
            "", person_evidence,
            person_name=name, selected_signals=signals,
        )
        business = assess_business_model(company, company_context, business_model)
        gcc = assess_gcc(company, company_context, gcc_only)
        location_evidence = extract_profile_location(
            profile_body, title, locations,
            require_current_evidence=True, person_name=name,
        )
        hits = {
            "role": check_role_in_title(designation, "", roles),
            "location": check_location_in_snippet(
                profile_body, title, href, locations,
                require_current_evidence=True, person_name=name,
            ),
            "organization": any_term_matches(company, organization_terms) if organization_terms else True,
            "industry": (
                bool(normalize_text(company_evidence))
                and any_term_matches(company_context, industries)
            ) if industries else True,
            "signal": (
                bool(normalize_text(person_evidence)) and signal["selected_match"]
            ) if signals else True,
            "custom": (
                bool(normalize_text(company_evidence))
                and any_term_matches(company_context, custom_terms)
            ) if custom_terms else True,
            "business_model": (
                business["matched"]
                and (business["desired"] == "Any" or bool(normalize_text(company_evidence)))
            ),
            "gcc": (
                bool(normalize_text(company_evidence)) and gcc["matched"]
            ) if gcc_only else True,
        }
        hard = all(hits[key] for key in ("role", "location", "organization"))
        strict_keys = ("industry", "signal", "custom", "business_model", "gcc")
        strict = hard and (all(hits[key] for key in strict_keys) if strict_stage else True)
        validation_text = combined_text(profile_text, company_evidence, person_evidence)
        matches = get_parameter_matches(
            validation_text, roles, locations, industries, signals,
            custom_terms, organization_terms,
        )
        matches["Role"] = get_parameter_matches(designation, roles, [], [], [], [], [])["Role"]
        matches["Location"] = get_parameter_matches(location_evidence, [], locations, [], [], [], [])["Location"]
        matches["Role_Evidence"] = [designation] if designation else []
        matches["Location_Evidence"] = [location_evidence] if location_evidence else []
        matches["Business_Model"] = [business["detected"]] if business["detected"] != "Unknown" else []
        matches["GCC"] = [gcc["evidence"]] if gcc["matched"] else []
        if signals:
            matches["Signal"] = [signal["name"]] if signal["selected_match"] else []
        score = signal["score"]
        for hit, weight in (
            (hits["role"], self.scoring.role),
            (hits["location"], self.scoring.location),
            (bool(industries) and hits["industry"], self.scoring.industry),
            ((bool(custom_terms) and hits["custom"]) or (bool(organization_terms) and hits["organization"]), self.scoring.custom),
            (normalize_text(company) not in {"", "unknown"}, self.scoring.company_known),
            (business["desired"] != "Any" and business["matched"], self.scoring.business_model),
        ):
            if hit:
                score += weight
        score = max(self.scoring.minimum, min(self.scoring.maximum, int(score)))
        return CandidateEvaluation(
            hard_qualified=hard,
            strict_qualified=strict,
            hits=hits,
            matches=matches,
            signal=signal,
            business=business,
            gcc=gcc,
            score=score,
            evidence_text=validation_text,
        )


class LeadBuilder:
    def build(
        self,
        *,
        name: str,
        designation: str,
        company: str,
        linkedin_url: str,
        location: str,
        query_bucket: str,
        source: str,
        evaluation: CandidateEvaluation,
        roles: list[str],
        locations: list[str],
        industries: list[str],
        signals: list[str],
        custom_terms: list[str],
        organization_terms: list[str],
    ) -> LeadRecord:
        matches = evaluation.matches
        match_columns = {
            "Matched_Role": ", ".join(matches.get("Role", [])),
            "Matched_Location": ", ".join(matches.get("Location", [])),
            "Matched_Industry": ", ".join(matches.get("Industry", [])),
            "Matched_Signal": ", ".join(matches.get("Signal", [])),
            "Matched_Custom": ", ".join(matches.get("Custom", [])),
            "Matched_Organisation": ", ".join(matches.get("Organisation", [])),
            "Matched_Business_Model": ", ".join(matches.get("Business_Model", [])),
            "Matched_GCC": ", ".join(matches.get("GCC", [])),
            "Role_Evidence": ", ".join(matches.get("Role_Evidence", [])),
            "Location_Evidence": location,
        }
        signal = evaluation.signal
        business = evaluation.business
        gcc = evaluation.gcc
        legacy = {
            "Lead_Score": evaluation.score,
            "Query_Bucket": query_bucket,
            "Matched_Parameters": get_detailed_match_summary(
                evaluation.evidence_text, roles, locations, industries, signals,
                custom_terms, organization_terms,
            ),
            "Role_Verified": "Confirmed" if evaluation.hits.get("role") else "Check",
            "Industry_Verified": (
                "Confirmed" if industries and evaluation.hits.get("industry")
                else "Check" if industries else "Not requested"
            ),
            "Location_Verified": "Confirmed" if evaluation.hits.get("location") else "Check",
            "Event_Probability": f"{signal.get('score', 50)}%",
            "Signal": signal.get("name", "No Verified Signal"),
            "Signal_Type": signal.get("type", "none"),
            "Signal_Confidence": signal.get("confidence", "Low"),
            "Signal_Evidence": signal.get("evidence", ""),
            "Signal_Verified": "Confirmed" if evaluation.hits.get("signal") and signals else "Not requested" if not signals else "Check",
            "Business_Model": business.get("detected", "Unknown"),
            "Business_Model_Confidence": business.get("confidence", "Low"),
            "Business_Model_Evidence": business.get("evidence", ""),
            "Business_Model_Verified": "Confirmed" if evaluation.hits.get("business_model") and business.get("desired") != "Any" else "Not requested" if business.get("desired") == "Any" else "Check",
            "GCC_Focus": "Requested" if gcc.get("requested") else "Not requested",
            "GCC_Verified": "Confirmed" if gcc.get("matched") else "Check" if gcc.get("requested") else "Not requested",
            "GCC_Evidence": gcc.get("evidence", ""),
            **match_columns,
        }
        return LeadRecord(
            name=name,
            designation=designation,
            company=company,
            linkedin_url=linkedin_url,
            verified_location=location,
            score=evaluation.score,
            query_bucket=query_bucket,
            source=source,
            hard_qualified=evaluation.hard_qualified,
            strict_qualified=evaluation.strict_qualified,
            evaluation={"details": evaluation.as_dict(), "legacy": legacy},
        )

