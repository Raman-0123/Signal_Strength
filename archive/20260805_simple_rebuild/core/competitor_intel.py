import re
import time
from contextlib import contextmanager

from core.signal_intelligence import assess_signal
from core.utils import any_term_matches, check_location_in_snippet, combined_text, parse_profile
from lead_generator_cli import get_event_probability_score


@contextmanager
def _search_context(search_client=None):
    if search_client is not None:
        yield search_client
        return
    from ddgs import DDGS
    with DDGS() as ddgs:
        yield ddgs


def extract_names_from_text(text: str) -> list:
    """
    Extract likely person names from post/snippet text.
    Uses capitalized word pairs/triples that look like names.
    Filters out common non-name capitalized words.
    """
    # Common words that appear capitalized but aren't names
    STOP_WORDS = {
        'the', 'and', 'for', 'with', 'from', 'our', 'this', 'that', 'are', 'was',
        'has', 'had', 'have', 'will', 'can', 'all', 'about', 'their', 'they',
        'been', 'more', 'who', 'how', 'what', 'when', 'why', 'not', 'but',
        'just', 'also', 'into', 'over', 'very', 'new', 'best', 'top',
        'linkedin', 'india', 'post', 'read', 'share', 'join', 'event',
        'roundtable', 'summit', 'conference', 'speaker', 'panelist',
        'marketing', 'digital', 'global', 'senior', 'chief', 'vice',
        'president', 'director', 'head', 'officer', 'manager', 'lead',
        'cmo', 'ceo', 'cto', 'cfo', 'coo', 'cro', 'ciso', 'chro',
        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
        'january', 'february', 'march', 'april', 'may', 'june', 'july',
        'august', 'september', 'october', 'november', 'december',
        'register', 'rsvp', 'attend', 'click', 'link', 'here', 'now',
        'fireside', 'chat', 'panel', 'discussion', 'keynote', 'talk',
        'industry', 'leaders', 'leadership', 'business', 'growth',
        'brand', 'strategy', 'innovation', 'technology', 'future',
        'insights', 'trends', 'data', 'driven', 'experience',
    }

    names = []
    # Pattern: 2-3 consecutive capitalized words (First Last or First Middle Last)
    name_pattern = re.findall(r'\b([A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15}){1,2})\b', text)
    for candidate in name_pattern:
        words = candidate.split()
        # Skip if any word is a stop word
        if any(w.lower() in STOP_WORDS for w in words):
            continue
        # Must have at least 2 words, each 2+ chars
        if len(words) >= 2 and all(len(w) >= 2 for w in words):
            names.append(candidate)

    # Also match "FirstName LastInitial." patterns like "Kishan P."
    initial_pattern = re.findall(r'\b([A-Z][a-z]{1,15}\s+[A-Z]\.)', text)
    for candidate in initial_pattern:
        first = candidate.split()[0]
        if first.lower() not in STOP_WORDS:
            names.append(candidate)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for n in names:
        n_clean = n.strip()
        if n_clean.lower() not in seen and len(n_clean) > 3:
            seen.add(n_clean.lower())
            unique.append(n_clean)

    return unique


def extract_event_participants(text: str, event_kws: list) -> list:
    """Keep only names with contextual evidence of event participation."""
    participants = []
    for name in extract_names_from_text(text):
        match = re.search(re.escape(name), text, re.IGNORECASE)
        if not match:
            continue
        before = text[max(0, match.start() - 110): match.start()]
        after = text[match.end(): match.end() + 80]
        listed_as_participant = re.search(
            r"(?:speakers?|panelists?|participants?|attendees?|honou?rees?|winners?)"
            r"\s*(?:include|included|are|were|:|-)|"
            r"(?:roundtable|panel|summit|conference)\s+(?:with|featuring)",
            before,
            re.IGNORECASE,
        )
        described_as_participant = re.match(
            r"[^.;]{0,35}\b(?:is|was|will be|joined|attended|spoke|participated|"
            r"moderated|hosted)\b[^.;]{0,45}\b(?:speaker|panelist|roundtable|"
            r"summit|conference|award|honou?ree|winner)\b",
            after,
            re.IGNORECASE,
        )
        if not listed_as_participant and not described_as_participant:
            continue
        context = text[max(0, match.start() - 170): match.end() + 170]
        signal = assess_signal(
            "", context, person_name=name, selected_signals=event_kws,
        )
        if event_kws and not signal["selected_match"]:
            continue
        if signal["name"] == "No Verified Signal":
            continue
        participants.append({
            "name": name,
            "signal": signal,
            "evidence": signal["evidence"],
        })
    return participants


def find_competitor_event_posts(company: str, event_kws: list, status_ui, search_client=None) -> list:
    """
    Step 1: Search DDG for competitor's LinkedIn posts about events/roundtables.
    Returns list of {title, body, href} dicts from post pages.
    """
    posts = []
    try:
        event_str = " OR ".join([f'"{e}"' if ' ' in e else e for e in event_kws[:8]])

        queries = [
            # LinkedIn posts from company about events
            f'site:linkedin.com/posts "{company}" ({event_str})',
            f'site:linkedin.com/feed "{company}" ({event_str})',
            f'site:linkedin.com "{company}" roundtable speakers panelists attendees',
            f'site:linkedin.com "{company}" event "{event_str}" congratulations',
            # Activity/pulse pages that list attendees
            f'site:linkedin.com "{company}" roundtable attendees panelist',
            f'site:linkedin.com/pulse "{company}" ({event_str})',
            # General web results mentioning company events with names
            f'"{company}" roundtable attendees speakers list linkedin',
            f'"{company}" event panelists speakers names',
            f'"{company}" CMO roundtable participants',
        ]

        seen_urls = set()
        with _search_context(search_client) as ddgs:
            for qidx, q in enumerate(queries):
                if len(posts) >= 30:
                    break
                try:
                    results = list(ddgs.text(q, max_results=20))
                    for r in results:
                        href = r.get('href', '')
                        if href in seen_urls:
                            continue
                        seen_urls.add(href)
                        body = r.get('body', '')
                        title = r.get('title', '')
                        # We want posts/pages that contain actual names
                        if len(body) > 40:
                            posts.append({
                                'title': title,
                                'body': body,
                                'href': href,
                            })
                except Exception:
                    if search_client is None:
                        time.sleep(0.5)
                    continue
                if search_client is None:
                    time.sleep(1)

    except Exception as e:
        status_ui.write(f"Error finding posts for {company}: {e}")
    return posts


def resolve_name_to_profile(name: str, locs: list, existing_urls: set, search_client=None) -> dict:
    """
    Step 3: Given an extracted name, find their LinkedIn profile via DDG.
    Returns a lead dict or None.
    """
    try:
        loc_str = " OR ".join([f'"{l}"' if ' ' in l else l for l in locs[:3]]) if locs else ""
        queries = [f'site:linkedin.com/in "{name}"']
        if loc_str:
            queries.insert(0, f'site:linkedin.com/in "{name}" ({loc_str})')

        with _search_context(search_client) as ddgs:
            for q in queries:
                try:
                    results = list(ddgs.text(q, max_results=5))
                    for r in results:
                        href = r.get('href', '')
                        title = r.get('title', '')
                        body = r.get('body', '')
                        if 'linkedin.com/in/' not in href:
                            continue
                        parsed_name, designation, company, clean_url = parse_profile(title, href, body)
                        if not clean_url or clean_url in existing_urls:
                            continue
                        # Verify the name roughly matches
                        name_parts = name.lower().split()
                        name_hints = [name]
                        if len(name_parts) >= 2:
                            name_hints.append(" ".join(name_parts[:2]))
                        if any_term_matches(combined_text(title, body), name_hints):
                            loc_confirmed = check_location_in_snippet(body, title, href, locs)
                            sig_score, sig_name = get_event_probability_score(
                                body, title, person_name=parsed_name or name,
                            )
                            return {
                                "name": parsed_name or name,
                                "designation": designation,
                                "company": company,
                                "url": clean_url,
                                "loc_confirmed": loc_confirmed,
                                "sig_score": sig_score,
                                "sig_name": sig_name,
                            }
                except Exception:
                    continue
                if search_client is None:
                    time.sleep(0.5)
    except Exception:
        pass
    return None


def scrape_competitor_event_attendees(
    competitors: list, roles: list, locs: list, event_kws: list,
    count: int, existing_urls: set, status_ui, search_client=None
) -> list:
    """
    3-Step Pipeline:
    1. Find competitor's LinkedIn posts/pages about events/roundtables
    2. Extract person names mentioned in post content
    3. Resolve each name to a LinkedIn profile, filtered by location
    """
    leads = []
    try:
        def _join_or(items):
            return " OR ".join([f'"{i}"' if ' ' in i else i for i in items])

        flat_roles = [r.lower() for r in roles]

        for comp_idx, company in enumerate(competitors):
            if len(leads) >= count:
                break
            company = company.strip()
            if not company:
                continue

            status_ui.write("")
            status_ui.write(f"**━━━ [{comp_idx+1}/{len(competitors)}] Competitor: {company} ━━━**")

            # ── STEP 1: Find event posts ──
            status_ui.write(f"   **Step 1:** Searching for {company}'s event/roundtable posts...")
            posts = find_competitor_event_posts(company, event_kws, status_ui, search_client)
            status_ui.write(f"   Found **{len(posts)}** post/page snippets mentioning {company}")

            # ── STEP 2: Extract names from posts ──
            status_ui.write("   **Step 2:** Extracting person names from post content...")
            all_names = []
            for p in posts:
                text = p['title'] + " " + p['body']
                participants = extract_event_participants(text, event_kws)
                for participant in participants:
                    all_names.append({
                        "name": participant["name"],
                        "source_post": p['href'][:80],
                        "source_signal": participant["signal"],
                        "source_evidence": participant["evidence"],
                    })

            # Deduplicate names
            seen_names = set()
            unique_names = []
            for item in all_names:
                n_lower = item["name"].lower()
                if n_lower not in seen_names:
                    seen_names.add(n_lower)
                    unique_names.append(item)

            status_ui.write(f"   Extracted **{len(unique_names)}** unique person names")
            if unique_names:
                sample = ", ".join([n["name"] for n in unique_names[:8]])
                status_ui.write(f"   Sample: `{sample}`{'...' if len(unique_names) > 8 else ''}")

            # ── STEP 3: Resolve names to LinkedIn profiles ──
            status_ui.write(f"   **Step 3:** Resolving names to LinkedIn profiles (location: `{'`, `'.join(locs[:3])}`)...")

            resolved_count = 0
            for nidx, item in enumerate(unique_names):
                if len(leads) >= count:
                    break
                name = item["name"]

                if (nidx + 1) % 5 == 0 or nidx == 0:
                    status_ui.write(f"      Resolving {nidx+1}/{len(unique_names)}: `{name}` | Total leads: {len(leads)}/{count}")

                profile = resolve_name_to_profile(name, locs, existing_urls, search_client)
                if profile:
                    role_hit = not flat_roles or any_term_matches(
                        profile.get("designation", ""), flat_roles,
                    )

                    match_score = int(role_hit) + int(profile["loc_confirmed"])
                    if match_score == 0:
                        continue

                    existing_urls.add(profile["url"])
                    source_signal = item["source_signal"]
                    leads.append({
                        "Full_Name": profile["name"],
                        "Designation": profile["designation"],
                        "Company": profile["company"],
                        "LinkedIn_URL": profile["url"],
                        "Competitor_Source": company,
                        "Source_Post": item["source_post"],
                        "Event_Signal": source_signal["name"],
                        "Event_Probability": f"{source_signal['score']}%",
                        "Signal_Confidence": source_signal["confidence"],
                        "Signal_Evidence": item["source_evidence"],
                        "Location_Verified": "Yes" if profile["loc_confirmed"] else "Check",
                        "Lead_Score": source_signal["score"],
                        "Query_Bucket": "competitor_event",
                        "Matched_Parameters": f"Role={'Yes' if role_hit else 'Check'} | Location={'Yes' if profile['loc_confirmed'] else 'Check'} | MatchScore={match_score}/2",
                    })
                    resolved_count += 1
                if search_client is None:
                    time.sleep(0.8)

            status_ui.write(f"   **{company}** -> Resolved {resolved_count} profiles | Total: {len(leads)}/{count}")

            # ── BONUS: Also do direct LinkedIn profile search for this competitor ──
            status_ui.write(f"   **Bonus:** Direct LinkedIn search for {company} event attendees...")
            role_str = f"({_join_or(roles)})" if roles else ""
            loc_str  = f"({_join_or(locs)})"  if locs  else ""
            event_str = f"({_join_or(event_kws[:6])})" if event_kws else ""

            bonus_queries = [
                f'site:linkedin.com/in "{company}" {event_str} {role_str} {loc_str}',
                f'site:linkedin.com/in "{company}" {role_str} {loc_str}',
            ]
            with _search_context(search_client) as ddgs:
                for bq in bonus_queries:
                    if len(leads) >= count:
                        break
                    try:
                        results = list(ddgs.text(bq, max_results=30))
                        for r in results:
                            if len(leads) >= count:
                                break
                            href  = r.get('href', '')
                            title = r.get('title', '')
                            body  = r.get('body', '')
                            if 'linkedin.com/in/' not in href:
                                continue
                            pname, desig, comp, clean_url = parse_profile(title, href, body)
                            if not clean_url or clean_url in existing_urls:
                                continue
                            role_hit = not flat_roles or any_term_matches(desig, flat_roles)
                            loc_confirmed = check_location_in_snippet(body, title, href, locs)
                            if flat_roles and not role_hit:
                                continue
                            if locs and not loc_confirmed:
                                continue
                            signal = assess_signal(
                                title, body, person_name=pname or "",
                                selected_signals=event_kws,
                            )
                            if event_kws and not signal["selected_match"]:
                                continue
                            if signal["name"] == "No Verified Signal":
                                continue
                            existing_urls.add(clean_url)
                            leads.append({
                                "Full_Name": pname or "Unknown",
                                "Designation": desig,
                                "Company": comp,
                                "LinkedIn_URL": clean_url,
                                "Competitor_Source": company,
                                "Source_Post": "Direct LinkedIn search",
                                "Event_Signal": signal["name"],
                                "Event_Probability": f"{signal['score']}%",
                                "Signal_Confidence": signal["confidence"],
                                "Signal_Evidence": signal["evidence"],
                                "Location_Verified": "Yes" if loc_confirmed else "Check",
                                "Lead_Score": signal["score"],
                                "Query_Bucket": "competitor_bonus",
                                "Matched_Parameters": f"Role={'Yes' if role_hit else 'Check'} | Location={'Yes' if loc_confirmed else 'Check'}",
                            })
                    except Exception:
                        if search_client is None:
                            time.sleep(0.5)
                    if search_client is None:
                        time.sleep(1)

        status_ui.write("")
        status_ui.write(f"**Pipeline complete! {len(leads)} total profiles resolved.**")

    except Exception as e:
        status_ui.write(f"Competitor scrape error: {e}")
    return leads


def fetch_company_summary_batch(companies: list, status_ui=None, search_client=None) -> dict:
    """
    Given a list of company names, return a dict:
    {company_name: {"description": ..., "net_profit": ...}}
    Uses DDG to find a brief 1-2 sentence overview + net profit figure.
    """
    summaries = {}
    try:
        unique_companies = list({c.strip() for c in companies if c and c.strip() and c.lower() != "unknown"})

        with _search_context(search_client) as ddgs:
            for comp in unique_companies:
                if status_ui:
                    status_ui.write(f"   Fetching company profile: **{comp}**...")
                desc = ""
                profit = ""
                try:
                    # Description query
                    q_desc = f'"{comp}" company overview what they do services products'
                    results_d = list(ddgs.text(q_desc, max_results=3))
                    for r in results_d:
                        body = r.get("body", "")
                        if len(body) > 60 and not desc:
                            # Try to grab the first clean sentence
                            sentences = re.split(r'(?<=[.!?])\s+', body)
                            clean_sents = [s for s in sentences if len(s) > 30]
                            if clean_sents:
                                desc = " ".join(clean_sents[:2])[:300]
                            break

                    # Net profit query
                    q_profit = f'"{comp}" net profit revenue annual financial results'
                    results_p = list(ddgs.text(q_profit, max_results=3))
                    for r in results_p:
                        body = r.get("body", "")
                        m = re.search(
                            r'(?:net profit|net income|profit after tax)[^₹$\d]*[₹$]?\s*([\d,]+(?:\.\d+)?\s*(?:crore|million|billion|lakh|Cr|Mn|Bn|cr)?)',
                            body, re.IGNORECASE
                        )
                        if m and not profit:
                            profit = m.group(1).strip()
                            break

                    summaries[comp] = {
                        "description": desc or "—",
                        "net_profit": profit or "—",
                    }
                except Exception:
                    summaries[comp] = {"description": "—", "net_profit": "—"}
                if search_client is None:
                    time.sleep(0.5)
    except Exception as e:
        if status_ui:
            status_ui.write(f"Company summary error: {e}")
    return summaries
