import re
import time
import urllib.parse
from contextlib import nullcontext
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

from core.google_search import GoogleSecurityCheck, google_text_search
from core.query_builder import (
    build_company_validation_query,
    build_person_validation_query,
    build_query_plan,
    query_budget_for,
)
from core.signal_intelligence import (
    assess_business_model,
    assess_gcc,
    assess_signal,
    normalize_business_model,
)
from core.utils import (
    any_term_matches,
    check_location_in_snippet,
    check_role_in_title,
    combined_text,
    company_name_variants,
    extract_profile_location,
    get_detailed_match_summary,
    get_parameter_matches,
    is_export_ready_profile,
    lead_signal_text,
    normalize_linkedin_url,
    normalize_text,
    parse_profile,
    person_identity_key,
    score_web_profile,
    split_csv_terms,
)


def _ddgs_text_search(ddgs, query, *, page=1, max_results=50):
    """Search DuckDuckGo with automatic fallback backends and retry logic."""
    kwargs = {
        "region": "in-en",
        "safesearch": "off",
        "max_results": max_results,
        "page": page,
    }
    
    last_error = None
    # Try up to 3 times with increasing backoff
    for attempt in range(3):
        try:
            # Alternate backends on retries to bypass blocks
            backend = "api" if attempt == 0 else "auto" if attempt == 1 else "html"
            return list(ddgs.text(query, backend=backend, **kwargs))
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # Sleep 2s, then 4s
    
    raise last_error


def _search_pages_for_bucket(bucket):
    """DDGS discovery may use page two; Google follows its real Next link."""
    if bucket in {"discovery", "custom"}:
        return 2
    return 1


def _merge_partial_profile(partial_profiles, name, designation, company,
                           clean_url, title="", body=""):
    """Merge complementary search snippets for the same LinkedIn profile."""
    cached = partial_profiles.get(clean_url, {})
    cached_designation = cached.get("designation", "")
    designation_candidates = [
        candidate for candidate in (designation, cached_designation) if candidate
    ]
    merged_designation = max(
        designation_candidates,
        key=lambda value: len(normalize_text(value).split()),
        default="",
    )
    merged = {
        "name": (
            name
            if normalize_text(name) not in {"", "unknown", "none", "nan"}
            else cached.get("name", name)
        ),
        "designation": merged_designation,
        "company": (
            company
            if normalize_text(company) not in {"", "unknown", "none", "nan"}
            else cached.get("company", company)
        ),
        # Put the newest (often name-targeted enrichment) card first so its
        # structured Location/Experience fields remain inside the bounded
        # evidence window. Older complementary evidence is retained after it.
        "title": combined_text(title, cached.get("title", ""))[:420],
        "body": combined_text(body, cached.get("body", ""))[:840],
    }
    partial_profiles[clean_url] = merged
    return merged


def _evaluate_candidate(title, body, href, all_locs, all_roles, all_inds,
                        all_sigs, custom_terms, organization_terms,
                        business_model="Any", person_name="",
                        return_intelligence=False, evidence_policy="strict",
                        gcc_only=False, current_designation=None,
                        current_company=None, company_evidence="",
                        person_evidence=""):
    """Evaluate a search result against the selected filters.

    ``strict`` is retained for structured/local records and direct callers.
    ``search`` hard-gates role, selected location, organisation, and every
    requested validation category. Company and person evidence may come from
    the cached second-stage Google searches rather than the discovery snippet.
    """
    # DDGS can occasionally concatenate several LinkedIn cards into a single
    # title/body.  Restrict verification to the card associated with this href
    # so a later person's CTO/GCC/location text cannot qualify the first URL.
    profile_title = re.split(r"\|\s*LinkedIn", str(title or ""), maxsplit=1, flags=re.IGNORECASE)[0]
    # Keep evidence tied to the current result. Some search providers
    # concatenate multiple LinkedIn cards; a bounded profile-sized window
    # prevents the next person's role/location from qualifying this URL.
    profile_body = str(body or "")[:420]
    parsed_text = combined_text(profile_title, profile_body)
    if evidence_policy == "search":
        company_context = combined_text(
            current_company or "", company_evidence,
        )
        person_context = person_evidence
        signal_title = ""
    else:
        company_context = combined_text(
            current_company or "", company_evidence, parsed_text,
        )
        person_context = combined_text(profile_body, person_evidence)
        signal_title = profile_title
    validation_text = combined_text(parsed_text, company_evidence, person_evidence)
    signal = assess_signal(
        signal_title, person_context,
        person_name=person_name, selected_signals=all_sigs,
    )
    business = assess_business_model(
        current_company or profile_title, company_context, business_model,
    )
    gcc = assess_gcc(current_company or profile_title, company_context, gcc_only)
    current_role_text = (
        current_designation if current_designation is not None
        else combined_text(profile_title, profile_body)
    )
    current_company_text = (
        current_company if current_company is not None
        else parsed_text
    )
    company_evidence_available = bool(normalize_text(company_evidence))
    person_evidence_available = bool(normalize_text(person_evidence))
    hits = {
        "role": check_role_in_title(
            current_role_text,
            "" if current_designation is not None else profile_body,
            all_roles,
        ),
        # A location is a hard profile gate. Accept a leading Google/LinkedIn
        # profile location or an explicit Location field, but never a loose
        # mention such as "hiring in Bangalore" elsewhere in the snippet.
        "location": check_location_in_snippet(
            profile_body, profile_title, href, all_locs,
            require_current_evidence=True,
            person_name=person_name,
        ),
        "industry": (
            any_term_matches(company_context, all_inds)
            and (evidence_policy != "search" or company_evidence_available)
        ) if all_inds else True,
        "signal": (
            signal["selected_match"]
            and (evidence_policy != "search" or person_evidence_available)
        ) if all_sigs else True,
        "custom": (
            any_term_matches(company_context, custom_terms)
            and (evidence_policy != "search" or company_evidence_available)
        ) if custom_terms else True,
        "organization": any_term_matches(
            current_company_text, organization_terms
        ) if organization_terms else True,
        "business_model": (
            business["matched"]
            and (
                business["desired"] == "Any"
                or evidence_policy != "search"
                or company_evidence_available
            )
        ),
        "gcc": (
            gcc["matched"]
            and (evidence_policy != "search" or company_evidence_available)
        ) if gcc_only else True,
    }
    if evidence_policy == "search":
        # A selected category is a hard gate. Values inside that category remain
        # OR alternatives, but no selected industry/signal/keyword/company/fit
        # may be silently ignored.
        accepted = all(hits.values())
    else:
        accepted = all(hits.values())
    matches = get_parameter_matches(
        validation_text, all_roles, all_locs, all_inds, all_sigs,
        custom_terms, organization_terms,
    )
    if current_designation is not None and all_roles:
        matches["Role"] = get_parameter_matches(
            current_designation, all_roles, [], [], [], [], [],
        )["Role"]
    if current_company is not None and organization_terms:
        matches["Organisation"] = get_parameter_matches(
            current_company, [], [], [], [], [], organization_terms,
        )["Organisation"]
    if all_sigs:
        matches["Signal"] = [signal["name"]] if signal["selected_match"] else []
    location_evidence = extract_profile_location(
        profile_body, profile_title, all_locs,
        require_current_evidence=True,
        person_name=person_name,
    )
    # No fallback: if the snippet doesn't mention a selected city, the person
    # is probably not from there.  This prevents foreigners leaking in.
    if all_locs:
        matches["Location"] = get_parameter_matches(
            location_evidence, [], all_locs, [], [], [], [],
        )["Location"]
    matches["Role_Evidence"] = [current_designation] if current_designation else []
    matches["Location_Evidence"] = [location_evidence] if location_evidence else []
    matches["Business_Model"] = (
        [business["detected"]] if business["detected"] != "Unknown" else []
    )
    matches["GCC"] = [gcc["evidence"]] if gcc["matched"] else []
    if return_intelligence:
        return accepted, hits, matches, validation_text, signal, business, gcc
    return accepted, hits, matches, validation_text


def _match_columns(matches):
    return {
        "Matched_Role": ", ".join(matches["Role"]),
        "Matched_Location": ", ".join(matches["Location"]),
        "Matched_Industry": ", ".join(matches["Industry"]),
        "Matched_Signal": ", ".join(matches["Signal"]),
        "Matched_Custom": ", ".join(matches["Custom"]),
        "Matched_Organisation": ", ".join(matches["Organisation"]),
        "Matched_Business_Model": ", ".join(matches.get("Business_Model", [])),
        "Matched_GCC": ", ".join(matches.get("GCC", [])),
        "Role_Evidence": ", ".join(matches.get("Role_Evidence", [])),
        "Location_Evidence": ", ".join(matches.get("Location_Evidence", [])),
    }


def _intelligence_columns(signal, business):
    return {
        "Event_Probability": f"{signal['score']}%",
        "Signal": signal["name"],
        "Signal_Type": signal["type"],
        "Signal_Confidence": signal["confidence"],
        "Signal_Evidence": signal["evidence"],
        "Signal_Verified": (
            "Confirmed"
            if signal["selected_match"] and signal["name"] != "No Verified Signal"
            else "Check"
        ),
        "Business_Model": business["detected"],
        "Business_Model_Confidence": business["confidence"],
        "Business_Model_Evidence": business["evidence"],
        "Business_Model_Verified": (
            "Not requested" if business["desired"] == "Any"
            else "Confirmed" if business["matched"]
            else "Check" if business["detected"] == "Unknown"
            else "Mismatch"
        ),
    }


def _gcc_columns(gcc):
    return {
        "GCC_Focus": "Requested" if gcc["requested"] else "Not requested",
        "GCC_Verified": "Confirmed" if gcc["matched"] else (
            "Check" if gcc["requested"] else "Not requested"
        ),
        "GCC_Evidence": gcc["evidence"],
    }


def _match_summary(parsed_text, all_roles, all_locs, all_inds, all_sigs,
                   custom_terms, organization_terms, business):
    summary = get_detailed_match_summary(
        parsed_text, all_roles, all_locs, all_inds, all_sigs,
        custom_terms, organization_terms,
    )
    if business["desired"] != "Any":
        business_part = f"Business Model: {business['detected']} ({business['confidence']})"
        summary = f"{summary} | {business_part}" if summary else business_part
    return summary


def _citation_evidence_text(results, required_term_groups=None):
    """Flatten citations attributable to every required entity group."""
    parts = []
    for result in results or []:
        citation_text = combined_text(
            result.get("title", ""), result.get("body", ""),
        )
        if required_term_groups and not all(
            any_term_matches(citation_text, term_group)
            for term_group in required_term_groups
            if term_group
        ):
            continue
        parts.append(citation_text)
    return combined_text(*parts)[:12000]


def _cached_google_evidence(
    cache,
    key,
    query,
    browser_state,
    status_ui,
    phase,
    required_term_groups=None,
):
    """Run one generic Google evidence query and cache it across candidates."""
    if key in cache:
        return cache[key].get("text", "")
    status_ui.write(f"      [{phase}] {query}")
    try:
        results = google_text_search(
            query,
            page=1,
            max_results=10,
            browser_state=browser_state,
            linkedin_only=False,
        )
    except GoogleSecurityCheck as check:
        check.phase = phase
        raise
    evidence = {
        "query": query,
        "text": _citation_evidence_text(
            results, required_term_groups=required_term_groups,
        ),
        "result_count": len(results),
    }
    cache[key] = evidence
    return evidence["text"]


def _candidate_validation_evidence(
    name,
    company,
    all_inds,
    all_sigs,
    custom_kws,
    business_model,
    gcc_only,
    company_evidence_cache,
    person_evidence_cache,
    browser_state,
    status_ui,
):
    """Collect at most one company query and one person-signal query."""
    company_evidence = ""
    person_evidence = ""
    company_filters_requested = bool(
        all_inds
        or split_csv_terms(custom_kws)
        or normalize_business_model(business_model) != "Any"
        or gcc_only
    )
    company_key = normalize_text(company)
    if company_filters_requested and company_key:
        company_query = build_company_validation_query(
            company,
            all_inds=all_inds,
            custom_kws=custom_kws,
            business_model=business_model,
            gcc_only=gcc_only,
        )
        company_evidence = _cached_google_evidence(
            company_evidence_cache,
            company_key,
            company_query,
            browser_state,
            status_ui,
            "company validation",
            required_term_groups=[company_name_variants(company)],
        )

    if all_sigs:
        person_key = f"{normalize_text(name)}|{company_key}"
        person_query = build_person_validation_query(
            name,
            company,
            all_sigs=all_sigs,
        )
        person_evidence = _cached_google_evidence(
            person_evidence_cache,
            person_key,
            person_query,
            browser_state,
            status_ui,
            "person signal validation",
            required_term_groups=[
                [name],
                company_name_variants(company),
            ],
        )
    return company_evidence, person_evidence


def engine_ddgs(all_locs, all_roles, all_inds, all_sigs, custom_kws, count,
                existing_urls, status_ui, organization_kws="", existing_people=None,
                business_model="Any", gcc_only=False):
    """DDGS multi-query engine with location + role validation."""
    leads = []
    if existing_people is None:
        existing_people = set()
    try:
        from ddgs import DDGS
        custom_terms = split_csv_terms(custom_kws)
        organization_terms = split_csv_terms(organization_kws)
        max_queries = query_budget_for(count, all_locs, all_roles, all_inds, all_sigs)

        query_plan = build_query_plan(
            all_locs, all_roles, all_inds, all_sigs, custom_kws,
            max_queries=max_queries, organization_kws=organization_kws,
            business_model=business_model, gcc_only=gcc_only,
        )
        status_ui.write(
            f"**[Engine 1 — DDGS]** {len(query_plan)} parameter-wise queries generated "
            f"(budget {max_queries})."
        )

        with DDGS() as ddgs:
            for idx, qinfo in enumerate(query_plan):
                if len(leads) >= count:
                    break
                q = qinfo["query"]
                status_ui.write(
                    f"   ↳ [{qinfo['bucket']}] Query {idx+1}/{len(query_plan)} | Leads: **{len(leads)}/{count}**"
                )
                try:
                    results = _ddgs_text_search(
                        ddgs, q, page=1, max_results=50,
                    )

                    for r in results:
                        if len(leads) >= count:
                            break
                        href  = r.get('href', '')
                        title = r.get('title', '')
                        body  = r.get('body', '')

                        if 'linkedin.com/in/' not in href:
                            continue

                        name, designation, company, clean_url = parse_profile(title, href, body)
                        person_key = person_identity_key(name, company)
                        if (not clean_url or clean_url in existing_urls or
                                (person_key and person_key in existing_people)):
                            continue
                        if not is_export_ready_profile(
                            name, company, clean_url, designation=designation,
                        ):
                            continue

                        accepted, hits, matches, parsed_text, signal, business, gcc = _evaluate_candidate(
                            title, body, href, all_locs, all_roles, all_inds,
                            all_sigs, custom_terms, organization_terms,
                            business_model=business_model, person_name=name or "",
                            return_intelligence=True, evidence_policy="search",
                            gcc_only=gcc_only, current_designation=designation,
                            current_company=company,
                        )
                            
                        if not accepted:
                            continue

                        final_score = score_web_profile(
                            role_hit=hits["role"],
                            location_hit=hits["location"],
                            industry_hit=bool(all_inds) and hits["industry"],
                            signal_score=signal["score"],
                            custom_hit=(
                                (bool(custom_terms) and hits["custom"]) or
                                (bool(organization_terms) and hits["organization"])
                            ),
                            company_known=company != "Unknown",
                            business_model_hit=(business["desired"] != "Any" and business["matched"]),
                        )

                        existing_urls.add(clean_url)
                        if person_key:
                            existing_people.add(person_key)
                        lead_dict = {
                            "Full_Name": name or "Unknown",
                            "Designation": designation,
                            "Company": company,
                            "LinkedIn_URL": clean_url,
                            "Lead_Score": final_score,
                            "Query_Bucket": qinfo["bucket"],
                            "Matched_Parameters": _match_summary(
                                parsed_text, all_roles, all_locs, all_inds, all_sigs,
                                custom_terms, organization_terms, business,
                            ),
                            "Role_Verified": "Confirmed" if hits["role"] else "Check",
                            "Industry_Verified": (
                                "Confirmed" if all_inds and hits["industry"]
                                else "Check" if all_inds else "Not requested"
                            ),
                            "Location_Verified": "Confirmed" if hits["location"] else "Check",
                            **_intelligence_columns(signal, business),
                            **_gcc_columns(gcc),
                            **_match_columns(matches),
                        }
                        leads.append(lead_dict)
                        if "tab1_leads" in st.session_state:
                            st.session_state.tab1_leads.append(lead_dict)
                        if "live_table" in st.session_state:
                            df = pd.DataFrame(st.session_state.tab1_leads)
                            st.session_state.live_table.dataframe(df, use_container_width=True)
                except Exception:
                    time.sleep(1)
                    continue
                time.sleep(0.35)

    except Exception as e:
        status_ui.write(f"DDGS error: {e}")
    return leads


def engine_brave(all_locs, all_roles, all_inds, all_sigs, custom_kws, count,
                 existing_urls, status_ui, organization_kws="", existing_people=None,
                 business_model="Any", gcc_only=False):
    """Brave Search HTML scraping fallback."""
    leads = []
    if existing_people is None:
        existing_people = set()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        custom_terms = split_csv_terms(custom_kws)
        organization_terms = split_csv_terms(organization_kws)
        max_queries = min(20, query_budget_for(count, all_locs, all_roles, all_inds, all_sigs))
        query_plan = build_query_plan(
            all_locs, all_roles, all_inds, all_sigs, custom_kws,
            max_queries=max_queries, organization_kws=organization_kws,
            business_model=business_model, gcc_only=gcc_only,
        )

        for idx, qinfo in enumerate(query_plan):
            if len(leads) >= count:
                break
            query = qinfo["query"]
            for page in range(2):
                if len(leads) >= count:
                    break
                url = f"https://search.brave.com/search?q={urllib.parse.quote_plus(query)}&offset={page * 10}"
                resp = requests.get(url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, 'html.parser')
                for a in soup.find_all('a', href=True):
                    if len(leads) >= count:
                        break
                    href = a['href']
                    if 'linkedin.com/in/' not in href:
                        continue
                    snippet = a.get_text(" ", strip=True)
                    name, designation, company, clean_url = parse_profile(snippet, href, snippet)
                    person_key = person_identity_key(name, company)
                    if (not clean_url or clean_url in existing_urls or
                            (person_key and person_key in existing_people)):
                        continue
                    if not is_export_ready_profile(
                        name, company, clean_url, designation=designation,
                    ):
                        continue

                    accepted, hits, matches, parsed_text, signal, business, gcc = _evaluate_candidate(
                        snippet, "", href, all_locs, all_roles, all_inds,
                        all_sigs, custom_terms, organization_terms,
                        business_model=business_model, person_name=name or "",
                        return_intelligence=True, evidence_policy="search",
                        gcc_only=gcc_only, current_designation=designation,
                        current_company=company,
                    )
                        
                    if not accepted:
                        continue
                    existing_urls.add(clean_url)
                    if person_key:
                        existing_people.add(person_key)
                    lead_dict = {
                        "Full_Name": name or "Unknown",
                        "Designation": designation,
                        "Company": company,
                        "LinkedIn_URL": clean_url,
                        "Lead_Score": score_web_profile(
                            role_hit=hits["role"],
                            location_hit=hits["location"],
                            industry_hit=bool(all_inds) and hits["industry"],
                            signal_score=signal["score"],
                            custom_hit=(
                                (bool(custom_terms) and hits["custom"]) or
                                (bool(organization_terms) and hits["organization"])
                            ),
                            company_known=company != "Unknown",
                            business_model_hit=(business["desired"] != "Any" and business["matched"]),
                        ),
                        "Query_Bucket": qinfo["bucket"],
                        "Matched_Parameters": _match_summary(
                            parsed_text, all_roles, all_locs, all_inds, all_sigs,
                            custom_terms, organization_terms, business,
                        ),
                        "Role_Verified": "Confirmed" if hits["role"] else "Check",
                        "Industry_Verified": (
                            "Confirmed" if all_inds and hits["industry"]
                            else "Check" if all_inds else "Not requested"
                        ),
                        "Location_Verified": "Confirmed" if hits["location"] else "Check",
                        **_intelligence_columns(signal, business),
                        **_gcc_columns(gcc),
                        **_match_columns(matches),
                    }
                    leads.append(lead_dict)
                    if "tab1_leads" in st.session_state:
                        st.session_state.tab1_leads.append(lead_dict)
                    if "live_table" in st.session_state:
                        df = pd.DataFrame(st.session_state.tab1_leads)
                        st.session_state.live_table.dataframe(df, use_container_width=True)
                time.sleep(1.25)
    except Exception as e:
        status_ui.write(f"Brave error: {e}")
    return leads


def engine_local_db(all_locs, all_roles, all_inds, all_sigs, custom_kws, count,
                    existing_urls, status_ui, organization_kws="", existing_people=None,
                    business_model="Any", gcc_only=False):
    """Local verified database fallback."""
    leads = []
    if existing_people is None:
        existing_people = set()
    try:
        seed_path = Path(__file__).resolve().parents[1] / "India_B2B_Lead_Intelligence_Database.csv"
        DB = pd.read_csv(seed_path).fillna("").to_dict(orient="records")
        flat_roles = [r.lower() for r in all_roles]
        flat_locs  = [l.lower() for l in all_locs]
        flat_inds  = [i.lower() for i in all_inds]
        flat_sigs  = [s.lower() for s in all_sigs]
        custom_terms = split_csv_terms(custom_kws)
        organization_terms = split_csv_terms(organization_kws)

        for lead in DB:
            if len(leads) >= count:
                break
            linkedin = normalize_linkedin_url(lead.get("LinkedIn_URL", ""))
            person_key = person_identity_key(
                lead.get("Full_Name", ""), lead.get("Company", "")
            )
            if (not linkedin or linkedin in existing_urls or
                    (person_key and person_key in existing_people)):
                continue
            if not is_export_ready_profile(
                lead.get("Full_Name", ""),
                lead.get("Company", ""),
                linkedin,
                designation=lead.get("Designation", ""),
            ):
                continue
            role_text = combined_text(
                lead.get("Designation", ""),
                lead.get("Department", ""),
                lead.get("Decision_Authority", ""),
                lead.get("Seniority_Level", ""),
            )
            loc_text  = combined_text(
                lead.get("Location", ""),
                lead.get("City", ""),
                lead.get("State", ""),
                lead.get("HQ", ""),
                lead.get("Country", ""),
            )
            ind_text  = combined_text(
                lead.get("Industry", ""),
                lead.get("Sub_Industry", ""),
                lead.get("Company_Type", ""),
                lead.get("Company_Stage", ""),
            )
            signal_text = lead_signal_text(lead)
            profile_text = combined_text(
                lead.get("Full_Name", ""),
                lead.get("Company", ""),
                lead.get("Tech_Stack", ""),
                lead.get("Buying_Signals", ""),
                signal_text,
            )
            signal = assess_signal(
                role_text, signal_text,
                person_name=lead.get("Full_Name", ""),
                selected_signals=all_sigs,
            )
            business = assess_business_model(
                ind_text, profile_text, business_model,
            )
            gcc = assess_gcc(ind_text, profile_text, gcc_only)

            role_ok = any_term_matches(role_text, flat_roles) if flat_roles else True
            loc_ok  = any_term_matches(loc_text, flat_locs) if flat_locs else True
            ind_ok  = any_term_matches(ind_text, flat_inds) if flat_inds else True
            sig_ok  = signal["selected_match"] if flat_sigs else True
            custom_ok = any_term_matches(profile_text, custom_terms) if custom_terms else True
            organization_ok = any_term_matches(
                combined_text(lead.get("Company", ""), profile_text), organization_terms
            ) if organization_terms else True
            business_ok = business["matched"]
            gcc_ok = gcc["matched"] if gcc_only else True
            match_score = sum([
                role_ok, loc_ok, ind_ok, sig_ok, custom_ok, organization_ok,
                business_ok, gcc_ok,
            ])

            # Selected categories are mandatory; values within a category remain OR.
            if not all([
                role_ok, loc_ok, ind_ok, sig_ok, custom_ok, organization_ok,
                business_ok, gcc_ok,
            ]):
                continue

            if role_ok and loc_ok:
                detail_text = combined_text(role_text, loc_text, ind_text, signal_text, profile_text)
                matches = get_parameter_matches(
                    detail_text, all_roles, all_locs, all_inds, all_sigs,
                    custom_terms, organization_terms,
                )
                if all_sigs:
                    matches["Signal"] = [signal["name"]] if signal["selected_match"] else []
                matches["Role_Evidence"] = [lead.get("Designation", "")]
                location_evidence = (
                    lead.get("Location", "")
                    or lead.get("City", "")
                    or lead.get("HQ", "")
                )
                matches["Location_Evidence"] = [location_evidence] if location_evidence else []
                matches["Business_Model"] = (
                    [business["detected"]] if business["detected"] != "Unknown" else []
                )
                matches["GCC"] = [gcc["evidence"]] if gcc["matched"] else []
                existing_urls.add(linkedin)
                if person_key:
                    existing_people.add(person_key)
                lead_dict = {
                    "Full_Name": lead.get("Full_Name", "Unknown"),
                    "Designation": lead.get("Designation", ""),
                    "Company": lead.get("Company", ""),
                    "LinkedIn_URL": linkedin,
                    "Lead_Score": lead.get("Confidence_Score", lead.get("Overall_Priority_Score", 85)),
                    "Query_Bucket": "local_db",
                    "Matched_Parameters": _match_summary(
                        detail_text, all_roles, all_locs, all_inds, all_sigs,
                        custom_terms, organization_terms, business,
                    ) + f" | MatchScore={match_score}/8",
                    "Role_Verified": "Confirmed" if role_ok else "Check",
                    "Industry_Verified": "Confirmed" if all_inds and ind_ok else "Not requested",
                    "Location_Verified": "Confirmed" if loc_ok else "Check",
                    **_intelligence_columns(signal, business),
                    **_gcc_columns(gcc),
                    **_match_columns(matches),
                }
                leads.append(lead_dict)
                if "tab1_leads" in st.session_state:
                    st.session_state.tab1_leads.append(lead_dict)
                if "live_table" in st.session_state:
                    df = pd.DataFrame(st.session_state.tab1_leads)
                    st.session_state.live_table.dataframe(df, use_container_width=True)
    except Exception as e:
        status_ui.write(f"Local DB error: {e}")
    return leads


def deterministic_harvest(all_locs, all_roles, all_inds, all_sigs, custom_kws,
                          count, existing_urls, status_ui, organization_kws="",
                          existing_people=None, business_model="Any", gcc_only=False):
    """Master controller — all engines fill toward target count."""
    total = []
    if existing_people is None:
        existing_people = set()

    def remaining():
        return count - len(total)

    # Engine 1 — DDGS
    batch = engine_ddgs(
        all_locs, all_roles, all_inds, all_sigs, custom_kws, remaining(),
        existing_urls, status_ui, organization_kws, existing_people,
        business_model=business_model, gcc_only=gcc_only,
    )
    total.extend(batch)
    status_ui.write(f"DDGS -> {len(batch)} leads | Total: {len(total)}/{count}")

    # Engine 2 — Brave
    if remaining() > 0:
        status_ui.write(f"[Engine 2 — Brave] Need {remaining()} more...")
        batch = engine_brave(
            all_locs, all_roles, all_inds, all_sigs, custom_kws, remaining(),
            existing_urls, status_ui, organization_kws, existing_people,
            business_model=business_model, gcc_only=gcc_only,
        )
        total.extend(batch)
        status_ui.write(f"Brave -> {len(batch)} leads | Total: {len(total)}/{count}")

    # Engine 3 — Local DB
    if remaining() > 0:
        status_ui.write(f"[Engine 3 — Local DB] Need {remaining()} more...")
        batch = engine_local_db(
            all_locs, all_roles, all_inds, all_sigs, custom_kws, remaining(),
            existing_urls, status_ui, organization_kws, existing_people,
            business_model=business_model, gcc_only=gcc_only,
        )
        total.extend(batch)
        status_ui.write(f"Local DB -> {len(batch)} leads | Total: {len(total)}/{count}")

    used = "DDG+Brave+LocalDB"
    return total[:count], used


def harvest_query_batch(all_locs, all_roles, all_inds, all_sigs, custom_kws,
                        query_plan, query_idx, batch_size, existing_urls, status_ui,
                        organization_kws="", max_new_leads=None,
                        existing_people=None, business_model="Any", gcc_only=False,
                        partial_profiles=None, search_provider="ddgs",
                        browser_state=None, company_evidence_cache=None,
                        person_evidence_cache=None, max_new_pocs=None):
    """
    Process a batch of queries from the plan starting at query_idx.
    Returns (new_leads, next_query_idx, done).
    This enables continuous, stoppable, session-state-driven scraping in Streamlit.
    """
    leads = []
    if existing_people is None:
        existing_people = set()
    if partial_profiles is None:
        partial_profiles = {}
    if company_evidence_cache is None:
        company_evidence_cache = {}
    if person_evidence_cache is None:
        person_evidence_cache = {}
    custom_terms = split_csv_terms(custom_kws)
    organization_terms = split_csv_terms(organization_kws)
    pocs_at_start = len(st.session_state.get("tab1_all_pocs", []))
    resume_result = (
        browser_state.get("resume_result")
        if isinstance(browser_state, dict)
        else None
    )
    resume_validation = {
        "pending": bool(
            isinstance(resume_result, dict)
            and resume_result.get("linkedin_only") is False
        ),
        "done": False,
    }

    def new_poc_count():
        return max(
            0,
            len(st.session_state.get("tab1_all_pocs", [])) - pocs_at_start,
        )

    def poc_target_reached():
        if resume_validation["pending"] and not resume_validation["done"]:
            return False
        return (
            max_new_pocs is not None
            and new_poc_count() >= max_new_pocs
        )

    if (
        max_new_pocs is not None
        and max_new_pocs <= 0
        and not resume_validation["pending"]
    ):
        return leads, query_idx, True
    if max_new_leads is not None and max_new_leads <= 0:
        return leads, query_idx, True
    end_idx = min(query_idx + batch_size, len(query_plan))

    provider = str(search_provider or "ddgs").strip().lower()
    if provider not in {"ddgs", "google"}:
        raise ValueError(f"Unsupported search provider: {search_provider}")
    if provider == "google" and browser_state is None:
        browser_state = {}

    try:
        if provider == "google":
            search_context = nullcontext(None)
        else:
            from ddgs import DDGS
            search_context = DDGS()
        with search_context as ddgs:
            for idx in range(query_idx, end_idx):
                if (
                    poc_target_reached()
                    or (
                        max_new_leads is not None
                        and len(leads) >= max_new_leads
                    )
                ):
                    break
                qinfo = query_plan[idx]
                q = qinfo["query"]
                leads_before_query = len(leads)
                status_ui.write(
                    f"   Query {idx+1}/{len(query_plan)} [{qinfo['bucket']}] | "
                    f"Found so far: **{len(st.session_state.get('tab1_leads', []))}**"
                )
                try:
                    page_count = _search_pages_for_bucket(qinfo["bucket"])
                    results_returned = 0
                    linkedin_candidates = 0
                    rejected = {
                        "duplicate": 0, "role": 0, "location": 0,
                        "organisation": 0, "business_mismatch": 0,
                        "industry": 0, "signal": 0, "custom": 0, "gcc": 0,
                        "incomplete": 0,
                    }
                    search_errors = []
                    if provider == "google":
                        progress_root = browser_state.setdefault(
                            "discovery_progress", {}
                        )
                        progress_key = (
                            f"{idx}:{normalize_text(q)}"
                        )
                        progress = progress_root.setdefault(
                            progress_key,
                            {
                                "active_query": q,
                                "kind": "primary",
                                "page": 1,
                                "pending_results": [],
                                "result_index": 0,
                                "pending_next_page": 0,
                                "after_request": None,
                                "seen_page_fingerprints": [],
                                "seen_result_urls": [],
                                "fallback_started": False,
                                "complete": False,
                            },
                        )
                        if progress.get("complete"):
                            page_requests = []
                        else:
                            page_requests = [{
                                "query": progress.get("active_query", q),
                                "page": int(progress.get("page", 1)),
                                "kind": progress.get("kind", "primary"),
                            }]
                    else:
                        progress = None
                        page_requests = [
                            {"query": q, "page": page, "kind": "primary"}
                            for page in range(1, page_count + 1)
                        ]

                    def queue_google_fallback(active_query, request_kind, reason):
                        if provider != "google" or request_kind != "primary":
                            return False
                        fallback_query = str(
                            qinfo.get("fallback_query", "")
                        ).strip()
                        if (
                            not fallback_query
                            or fallback_query == active_query
                            or progress.get("fallback_started")
                            or poc_target_reached()
                        ):
                            return False
                        fallback_request = {
                            "query": fallback_query,
                            "page": 1,
                            "kind": "fallback",
                        }
                        progress.update({
                            "active_query": fallback_query,
                            "kind": "fallback",
                            "page": 1,
                            "pending_results": [],
                            "result_index": 0,
                            "pending_next_page": 0,
                            "after_request": None,
                            "fallback_started": True,
                            "complete": False,
                        })
                        page_requests.append(fallback_request)
                        status_ui.write(
                            f"      {reason}; trying the strict alternate-title "
                            "query once."
                        )
                        return True

                    request_idx = 0
                    while request_idx < len(page_requests):
                        request = page_requests[request_idx]
                        request_idx += 1
                        page = request["page"]
                        active_query = request["query"]
                        request_kind = request["kind"]
                        status_ui.write(
                            f"      Exact Google query "
                            f"({request_kind}, page {page}): {active_query}"
                        )
                        if (
                            poc_target_reached()
                            or (
                                max_new_leads is not None
                                and len(leads) >= max_new_leads
                            )
                        ):
                            break
                        resumed_pending_page = bool(
                            provider == "google"
                            and progress.get("pending_results")
                            and progress.get("active_query") == active_query
                            and int(progress.get("page", 1)) == page
                            and progress.get("kind") == request_kind
                        )
                        try:
                            if resumed_pending_page:
                                results = list(
                                    progress.get("pending_results", [])
                                )
                            elif provider == "google":
                                results = google_text_search(
                                    active_query, page=page, max_results=10,
                                    browser_state=browser_state,
                                )
                            else:
                                results = _ddgs_text_search(
                                    ddgs, active_query, page=page, max_results=50,
                                )
                        except Exception as page_exc:
                            if isinstance(page_exc, GoogleSecurityCheck):
                                raise
                            err_type = type(page_exc).__name__
                            if err_type in ["TimeoutException", "DDGSException", "RatelimitException"]:
                                raise RuntimeError(f"RATE_LIMIT:{err_type}") from page_exc
                                
                            search_errors.append(
                                f"{request_kind} page {page}: {err_type}"
                            )
                            if provider == "google":
                                progress["complete"] = True
                            continue

                        results_returned += len(results)
                        if provider == "google" and not resumed_pending_page:
                            next_page = int(
                                browser_state.get(
                                    "last_search_next_page"
                                )
                                or 0
                            )
                            normalized_page_urls = [
                                normalize_linkedin_url(result.get("href", ""))
                                for result in results
                            ]
                            normalized_page_urls = [
                                value for value in normalized_page_urls if value
                            ]
                            fingerprint = "|".join(
                                sorted(set(normalized_page_urls))
                            )
                            seen_fingerprints = set(
                                progress.get("seen_page_fingerprints", [])
                            )
                            seen_result_urls = set(
                                progress.get("seen_result_urls", [])
                            )
                            new_results = [
                                result for result in results
                                if (
                                    normalize_linkedin_url(
                                        result.get("href", "")
                                    )
                                    not in seen_result_urls
                                )
                            ]
                            if (
                                results
                                and (
                                    (fingerprint and fingerprint in seen_fingerprints)
                                    or not new_results
                                )
                            ):
                                status_ui.write(
                                    "      Repeated Google result page detected; "
                                    "stopping this query family instead of "
                                    "looping over the same POCs."
                                )
                                progress["complete"] = True
                                progress["pending_results"] = []
                                if queue_google_fallback(
                                    active_query,
                                    request_kind,
                                    "Primary pagination repeated before the "
                                    "qualified-POC target",
                                ):
                                    continue
                                break

                            if results:
                                if fingerprint:
                                    progress.setdefault(
                                        "seen_page_fingerprints", []
                                    ).append(fingerprint)
                                progress["seen_result_urls"] = list(
                                    seen_result_urls
                                    | set(normalized_page_urls)
                                )
                                results = new_results
                                progress["pending_results"] = list(results)
                                progress["result_index"] = 0
                                progress["pending_next_page"] = next_page
                                progress["after_request"] = (
                                    {
                                        "query": active_query,
                                        "page": next_page,
                                        "kind": request_kind,
                                    }
                                    if next_page > page
                                    else None
                                )
                            elif page == 1 and request_kind == "primary":
                                if queue_google_fallback(
                                    active_query,
                                    request_kind,
                                    "Primary page 1 returned zero LinkedIn "
                                    "profiles",
                                ):
                                    continue
                                progress["complete"] = True
                                break
                            elif page == 1 and request_kind == "fallback":
                                status_ui.write(
                                    "      Alternate-title discovery also "
                                    "returned zero LinkedIn profiles; skipping "
                                    "this query family."
                                )
                                progress["complete"] = True
                                break
                            else:
                                progress["complete"] = True
                                if queue_google_fallback(
                                    active_query,
                                    request_kind,
                                    "Primary pagination was exhausted before "
                                    "the qualified-POC target",
                                ):
                                    continue
                                break

                        start_result_idx = (
                            int(progress.get("result_index", 0))
                            if provider == "google"
                            else 0
                        )
                        for result_offset, r in enumerate(
                            results[start_result_idx:],
                            start=start_result_idx,
                        ):
                            if (
                                poc_target_reached()
                                or (
                                    max_new_leads is not None
                                    and len(leads) >= max_new_leads
                                )
                            ):
                                break
                            if provider == "google":
                                # Advance before normal rejection/continue paths.
                                # A CAPTCHA rolls this back to retry the exact
                                # candidate without restarting discovery pages.
                                progress["result_index"] = result_offset + 1
                            href  = r.get('href', '')
                            title = r.get('title', '')
                            body  = r.get('body', '')
                            if not normalize_linkedin_url(href):
                                continue
                            linkedin_candidates += 1
                            name, designation, company, clean_url = parse_profile(
                                title, href, body
                            )
                            if not clean_url:
                                rejected["duplicate"] += 1
                                continue
                            merged = _merge_partial_profile(
                                partial_profiles,
                                name, designation, company, clean_url,
                                title=title, body=body,
                            )
                            name = merged["name"]
                            designation = merged["designation"]
                            company = merged["company"]
                            evidence_title = merged["title"]
                            evidence_body = merged["body"]
                            person_key = person_identity_key(name, company)
                            if (clean_url in existing_urls or
                                    (person_key and person_key in existing_people)):
                                rejected["duplicate"] += 1
                                continue
                            if not is_export_ready_profile(
                                name, company, clean_url,
                                designation=designation,
                            ):
                                rejected["incomplete"] += 1
                                continue

                            preaccepted, prehits, _, _ = _evaluate_candidate(
                                evidence_title, evidence_body, href,
                                all_locs, all_roles, [], [],
                                [], organization_terms,
                                business_model="Any", person_name=name or "",
                                evidence_policy="search",
                                current_designation=designation,
                                current_company=company,
                            )
                            if not preaccepted:
                                if not prehits["role"]:
                                    rejected["role"] += 1
                                if not prehits["location"]:
                                    rejected["location"] += 1
                                if not prehits["organization"]:
                                    rejected["organisation"] += 1
                                continue

                            location_evidence = extract_profile_location(
                                evidence_body,
                                evidence_title,
                                all_locs,
                                require_current_evidence=True,
                                person_name=name or "",
                            )
                            poc_dict = {
                                "Full_Name": name,
                                "Designation": designation,
                                "Company": company,
                                "LinkedIn_URL": clean_url,
                                "Role_Verified": "Confirmed",
                                "Location_Verified": "Confirmed" if location_evidence else "Check",
                                "Location_Evidence": location_evidence,
                            }
                            all_pocs = st.session_state.setdefault(
                                "tab1_all_pocs", []
                            )
                            existing_poc_urls = {
                                normalize_linkedin_url(
                                    item.get("LinkedIn_URL", "")
                                )
                                for item in all_pocs
                            }
                            if clean_url not in existing_poc_urls:
                                all_pocs.append(poc_dict)

                            company_evidence = ""
                            person_evidence = ""
                            if provider == "google":
                                try:
                                    company_evidence, person_evidence = (
                                        _candidate_validation_evidence(
                                            name,
                                            company,
                                            all_inds,
                                            all_sigs,
                                            custom_kws,
                                            business_model,
                                            gcc_only,
                                            company_evidence_cache,
                                            person_evidence_cache,
                                            browser_state,
                                            status_ui,
                                        )
                                    )
                                except GoogleSecurityCheck:
                                    progress["result_index"] = result_offset
                                    raise
                            accepted, hits, matches, parsed_text, signal, business, gcc = _evaluate_candidate(
                                evidence_title, evidence_body, href,
                                all_locs, all_roles, all_inds,
                                all_sigs, custom_terms, organization_terms,
                                business_model=business_model, person_name=name or "",
                                return_intelligence=True, evidence_policy="search",
                                gcc_only=gcc_only, current_designation=designation,
                                current_company=company,
                                company_evidence=company_evidence,
                                person_evidence=person_evidence,
                            )
                            if not accepted:
                                if not hits["role"]:
                                    rejected["role"] += 1
                                if not hits["location"]:
                                    rejected["location"] += 1
                                if not hits["organization"]:
                                    rejected["organisation"] += 1
                                if not hits["industry"]:
                                    rejected["industry"] += 1
                                if not hits["signal"]:
                                    rejected["signal"] += 1
                                if not hits["custom"]:
                                    rejected["custom"] += 1
                                if not hits["gcc"]:
                                    rejected["gcc"] += 1
                                if not hits["business_model"]:
                                    rejected["business_mismatch"] += 1
                                # The profile remains in All Qualified POCs,
                                # but it must not trigger the same strict
                                # evidence searches again in another query.
                                existing_urls.add(clean_url)
                                if person_key:
                                    existing_people.add(person_key)
                                if resume_validation["pending"]:
                                    resume_validation["done"] = True
                                continue

                            final_score = score_web_profile(
                                role_hit=hits["role"],
                                location_hit=hits["location"],
                                industry_hit=bool(all_inds) and hits["industry"],
                                signal_score=signal["score"],
                                custom_hit=(
                                    (bool(custom_terms) and hits["custom"]) or
                                    (bool(organization_terms) and hits["organization"])
                                ),
                                company_known=True,
                                business_model_hit=(
                                    business["desired"] != "Any" and business["matched"]
                                ),
                            )
                            existing_urls.add(clean_url)
                            if person_key:
                                existing_people.add(person_key)
                            lead_dict = {
                                "Full_Name": name,
                                "Designation": designation,
                                "Company": company,
                                "LinkedIn_URL": clean_url,
                                "Lead_Score": final_score,
                                "Query_Bucket": qinfo["bucket"],
                                "Matched_Parameters": _match_summary(
                                    parsed_text, all_roles, all_locs, all_inds, all_sigs,
                                    custom_terms, organization_terms, business,
                                ),
                                "Role_Verified": "Confirmed" if hits["role"] else "Check",
                                "Industry_Verified": (
                                    "Confirmed" if all_inds and hits["industry"]
                                    else "Check" if all_inds else "Not requested"
                                ),
                                "Location_Verified": "Confirmed" if hits["location"] else "Check",
                                **_intelligence_columns(signal, business),
                                **_gcc_columns(gcc),
                                **_match_columns(matches),
                            }
                            leads.append(lead_dict)
                            partial_profiles.pop(clean_url, None)
                            if "tab1_leads" in st.session_state:
                                st.session_state.tab1_leads.append(lead_dict)
                            if resume_validation["pending"]:
                                resume_validation["done"] = True
                        if (
                            provider == "google"
                            and int(progress.get("result_index", 0))
                            >= len(progress.get("pending_results", []))
                        ):
                            next_request = progress.get("after_request")
                            progress["pending_results"] = []
                            progress["result_index"] = 0
                            progress["after_request"] = None
                            if next_request:
                                progress.update({
                                    "active_query": next_request["query"],
                                    "kind": next_request["kind"],
                                    "page": next_request["page"],
                                })
                                page_requests.append(next_request)
                                status_ui.write(
                                    f"      Google Next detected — continuing "
                                    f"to page {next_request['page']}."
                                )
                            else:
                                if not queue_google_fallback(
                                    active_query,
                                    request_kind,
                                    "Primary pages were exhausted before the "
                                    "qualified-POC target",
                                ):
                                    progress["complete"] = True
                    if results_returned == 0:
                        status_ui.write(
                            "      Discovery exhausted with no LinkedIn profiles."
                        )
                    elif linkedin_candidates == 0:
                        status_ui.write(
                            f"      {results_returned} results, but none were LinkedIn profiles."
                        )
                    elif len(leads) == leads_before_query:
                        status_ui.write(
                            "      Candidates checked: "
                            f"{linkedin_candidates} · duplicate {rejected['duplicate']} · "
                            f"missing name/company {rejected['incomplete']} · "
                            f"role mismatch {rejected['role']} · "
                            f"location unverified {rejected['location']} · "
                            f"organisation mismatch {rejected['organisation']} · "
                            f"industry mismatch {rejected['industry']} · "
                            f"signal unverified {rejected['signal']} · "
                            f"custom mismatch {rejected['custom']} · "
                            f"GCC unverified {rejected['gcc']} · "
                            f"business-model mismatch {rejected['business_mismatch']}"
                        )
                    if search_errors and results_returned == 0:
                        status_ui.write(
                            "      Search pages unavailable: " + ", ".join(search_errors)
                        )
                except Exception as exc:
                    if isinstance(exc, GoogleSecurityCheck):
                        raise
                    err_type = type(exc).__name__
                    if err_type in ["TimeoutException", "DDGSException", "RatelimitException"]:
                        raise RuntimeError(f"RATE_LIMIT:{err_type}") from exc
                        
                    status_ui.write(
                        f"      Search engine error: {type(exc).__name__}: {exc}"
                    )
                    time.sleep(0.5)
                    continue
                time.sleep(0.35)

    except GoogleSecurityCheck:
        raise
    except Exception as e:
        if str(e).startswith("RATE_LIMIT:"):
            raise
        status_ui.write(f"Engine error: {e}")

    next_idx = end_idx
    done = next_idx >= len(query_plan) or (
        max_new_leads is not None and len(leads) >= max_new_leads
    ) or poc_target_reached()
    return leads, next_idx, done
