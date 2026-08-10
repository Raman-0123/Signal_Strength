import re
import time
from contextlib import contextmanager

from core.utils import (
    any_term_matches,
    check_role_in_title,
    combined_text,
    company_name_variants,
    parse_profile,
)
from lead_generator_cli import get_event_probability_score

DEFAULT_SENIOR_ROLES = [
    "CEO", "Chief Executive Officer", "Founder", "Co-Founder",
    "Managing Director", "President", "Vice President", "VP", "SVP", "AVP",
    "Director", "Chief", "Head", "Chairman", "Chairperson", "Partner",
]


@contextmanager
def _search_context(search_client=None):
    if search_client is not None:
        yield search_client
        return
    from ddgs import DDGS
    with DDGS() as ddgs:
        yield ddgs


def fetch_company_profile(company_name: str, search_client=None) -> dict:
    """Scrape company overview, size, revenue from web search."""
    profile = {
        "name": company_name,
        "description": "",
        "employees": "",
        "revenue": "",
        "profit": "",
        "industry": "",
        "founded": "",
        "hq": "",
        "linkedin_page": "",
        "website": "",
        "kpis": "",
        "top_leaders": "",
    }
    try:
        # Pull company card from DDG
        query = f'"{company_name}" company India revenue employees founded about'
        query_intel = f'"{company_name}" net profit top leaders key performance indicators KPI'
        with _search_context(search_client) as ddgs:
            results = list(ddgs.text(query, max_results=6))
            results_intel = list(ddgs.text(query_intel, max_results=5))

        for r in results:
            body = r.get('body', '')
            href = r.get('href', '')
            if not profile["description"] and len(body) > 60:
                profile["description"] = body[:500]
            # Extract revenue hints
            rev_match = re.search(r'(?:revenue|turnover|sales)[^\$₹\d]*[₹$]?\s*([\d,]+(?:\.\d+)?\s*(?:crore|million|billion|lakh|Cr|Mn|Bn)?)', body, re.IGNORECASE)
            if rev_match and not profile["revenue"]:
                profile["revenue"] = rev_match.group(1).strip()
            # Extract employee count
            emp_match = re.search(r'(\d[\d,]+)\s*(?:\+)?\s*(?:employees|workforce|staff|headcount|people)', body, re.IGNORECASE)
            if emp_match and not profile["employees"]:
                profile["employees"] = emp_match.group(1).replace(',', '')
            # Founded year
            found_match = re.search(r'(?:founded|established|incorporated)\s*(?:in)?\s*((?:19|20)\d{2})', body, re.IGNORECASE)
            if found_match and not profile["founded"]:
                profile["founded"] = found_match.group(1)
            # HQ
            hq_match = re.search(r'(?:headquartered|HQ|headquarters)\s+(?:in\s+)?([A-Za-z\s,]+?)(?:[\.;,]|$)', body, re.IGNORECASE)
            if hq_match and not profile["hq"]:
                profile["hq"] = hq_match.group(1).strip()[:50]
            # LinkedIn company page
            if 'linkedin.com/company/' in href and not profile["linkedin_page"]:
                profile["linkedin_page"] = href
            # Website
            if not profile["website"] and href and 'linkedin' not in href and 'ddg' not in href:
                profile["website"] = href

        for r in results_intel:
            body = r.get('body', '')
            # Extract profit
            prof_match = re.search(r'(?:net profit|profit|net income)[^\$₹\d]*[₹$]?\s*([\d,]+(?:\.\d+)?\s*(?:crore|million|billion|lakh|Cr|Mn|Bn)?)', body, re.IGNORECASE)
            if prof_match and not profile["profit"]:
                profile["profit"] = prof_match.group(1).strip()
            # Top leaders (just naive sentence grab)
            if any(k in body.lower() for k in ['ceo', 'founder', 'md', 'managing director', 'leader']) and not profile["top_leaders"]:
                profile["top_leaders"] = body[:250] + "..."
            # KPIs
            if any(k in body.lower() for k in ['growth', 'margin', 'ebitda', 'kpi', 'performance']) and not profile["kpis"]:
                profile["kpis"] = body[:250] + "..."

    except Exception as e:
        profile["description"] = f"Could not fetch company data: {e}"
    return profile


def scrape_company_employees(company_name: str, location: str, role_filter: list, count: int, existing_urls: set, status_ui, search_client=None) -> list:
    """Find employees at a specific company via DDG."""
    leads = []
    try:
        verified_roles = role_filter or DEFAULT_SENIOR_ROLES
        company_terms = company_name_variants(company_name)

        # Build targeted queries
        queries = []
        if location:
            base = f'site:linkedin.com/in "{company_name}" "{location}"'
        else:
            base = f'site:linkedin.com/in "{company_name}"'
        queries.append(base)

        if role_filter:
            role_str = " OR ".join([f'"{r}"' if ' ' in r else r for r in role_filter])
            if location:
                queries.append(f'site:linkedin.com/in "{company_name}" "{location}" ({role_str})')
            else:
                queries.append(f'site:linkedin.com/in "{company_name}" ({role_str})')
            # Per-role queries for freshness
            for role in role_filter[:6]:
                q_r = f'"{role}"' if ' ' in role else role
                if location:
                    queries.append(f'site:linkedin.com/in "{company_name}" "{location}" {q_r}')
                else:
                    queries.append(f'site:linkedin.com/in "{company_name}" {q_r}')
        else:
            # Senior leadership by default
            if location:
                queries.append(f'site:linkedin.com/in "{company_name}" "{location}" (CEO OR CMO OR CTO OR CFO OR COO OR VP OR Director OR Head)')
            else:
                queries.append(f'site:linkedin.com/in "{company_name}" (CEO OR CMO OR CTO OR CFO OR COO OR VP OR Director OR Head)')

        status_ui.write(f"Searching **{len(queries)}** queries for `{company_name}` employees...")

        with _search_context(search_client) as ddgs:
            for idx, q in enumerate(queries):
                if len(leads) >= count:
                    break
                status_ui.write(f"   Query {idx+1}/{len(queries)} | Found: {len(leads)}/{count}")
                try:
                    results = list(ddgs.text(q, max_results=50))
                    for r in results:
                        if len(leads) >= count:
                            break
                        href = r.get('href', '')
                        title = r.get('title', '')
                        body = r.get('body', '')
                        if 'linkedin.com/in/' not in href:
                            continue
                        name, designation, company, clean_url = parse_profile(title, href, body)
                        if not clean_url or clean_url in existing_urls:
                            continue

                        # Only the parsed current role/company may qualify a POC.
                        # Historical snippet text is useful as context but cannot
                        # prove that a person still works at the requested company.
                        company_hit = (
                            company != "Unknown"
                            and any_term_matches(company, company_terms)
                        )
                        # Track location when present, but do not hard-drop if it is missing.
                        loc_hit = True
                        if location:
                            loc_parts = [l.strip().lower() for l in location.split(',') if l.strip()]
                            if loc_parts:
                                text_to_search = combined_text(title, body)
                                loc_hit = any_term_matches(text_to_search, loc_parts)
                        loc_match = (
                            "Yes" if location and loc_hit
                            else "Check" if location
                            else "Not requested"
                        )
                        # Role filter: keep this strict so only the current designation/title qualifies.
                        # We intentionally do not fall back to arbitrary body text here, because that
                        # would let past experience or unrelated snippet text satisfy the filter.
                        role_hit = bool(designation) and check_role_in_title(
                            designation, "", verified_roles,
                        )

                        match_score = sum([company_hit, role_hit]) + (
                            int(loc_hit) if location else 0
                        )
                        max_score = 3 if location else 2
                        if not company_hit or not role_hit:
                            continue

                        sig_score, sig_name = get_event_probability_score(
                            body, title, person_name=name or "",
                        )
                        existing_urls.add(clean_url)
                        leads.append({
                            "Full_Name": name or "Unknown",
                            "Designation": designation,
                            "Company": company,
                            "Location_Match": loc_match,
                            "LinkedIn_URL": clean_url,
                            "Event_Probability": f"{sig_score}%",
                            "Signal": sig_name,
                            "Lead_Score": sig_score,
                            "Query_Bucket": "company_employee",
                            "Matched_Parameters": f"Company=Verified | Role=Verified | Location={loc_match} | MatchScore={match_score}/{max_score}",
                        })
                except Exception:
                    if search_client is None:
                        time.sleep(1)
                    continue
                if search_client is None:
                    time.sleep(1.5)
    except Exception as e:
        status_ui.write(f"Error: {e}")
    return leads
