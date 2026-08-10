from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

import streamlit as st
import yaml

# Ensure Playwright browser is installed (vital for Streamlit Cloud deployment)
os.system("playwright install chromium")

from speedy_scraper.background_jobs import (
    create_job,
    heartbeat_age_seconds,
    job_is_stale,
    launch_job,
    list_jobs,
    read_json,
    read_status,
    request_stop,
    write_json,
)
from speedy_scraper.config import load_catalog
from speedy_scraper.exports import leads_frame, rejections_frame
from speedy_scraper.lead_job import load_lead_job_checkpoint
from speedy_scraper.sources import independent_source_families
from speedy_scraper.ui import (
    action_button_css,
    captcha_recovery_panel,
    download_gsheet,
)

st.set_page_config(page_title="Speedy Scraper · Lead Operations", page_icon="◉", layout="wide")
st.markdown(action_button_css(), unsafe_allow_html=True)

first_paint = "ui_mounted" not in st.session_state
st.session_state.ui_mounted = True

# ─────────────────────────────────────────────────────────────────────────────
# Load configs
# ─────────────────────────────────────────────────────────────────────────────
catalog = load_catalog()
presets = dict(catalog.get("presets") or {})
source_defaults = dict(catalog.get("source_defaults") or {})
default_preset = str(catalog.get("default_preset") or next(iter(presets), ""))
# The preset builder was removed from this page, but jobs still persist the
# preset key so older checkpoints and the job archive remain compatible.
preset_name = default_preset

_config_dir = Path(__file__).parent / "config"


def _load_yaml(name: str) -> dict:
    path = _config_dir / name
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}


_location_tax: dict = _load_yaml("location_taxonomy.yaml").get("locations", {})
_role_tax: dict = _load_yaml("role_taxonomy.yaml").get("roles", {})

# Derived lookup structures
_all_locations: list[str] = list(_location_tax.keys())
_all_roles: list[str] = list(_role_tax.keys())
_regions_by_location: dict[str, str] = {
    loc: meta.get("region", "Other") for loc, meta in _location_tax.items()
}
_unique_regions: list[str] = sorted(set(_regions_by_location.values()))

_all_role_families: list[str] = sorted(
    set(v.get("role_family", "Other") for v in _role_tax.values() if isinstance(v, dict))
)

_industry_verticals: list[str] = [
    "FinTech",
    "SaaS / B2B Software",
    "HealthTech / Digital Health",
    "E-Commerce / Retail Tech",
    "AI / ML / GenAI",
    "EdTech",
    "CleanTech / Climate Tech",
    "Cybersecurity",
    "InsurTech",
    "PropTech",
    "HR Tech",
    "MarTech",
    "LegalTech",
    "AgriTech",
    "Logistics / Supply Chain Tech",
    "Gaming / Interactive Entertainment",
    "Biotech / Life Sciences",
    "DeepTech / Hardware",
    "Media / Content Tech",
    "Crypto / Web3 / Blockchain",
]

_industry_evidence_map: dict[str, list[str]] = {
    "FinTech": ["FinTech", "Payments", "Payment Technology", "Digital Payments", "WealthTech", "Lending", "Banking Technology", "Financial Services", "Neobank", "Digital Banking"],
    "SaaS / B2B Software": ["SaaS", "B2B Software", "Enterprise Software", "Cloud Computing", "DevOps", "Platform as a Service", "API Platform"],
    "HealthTech / Digital Health": ["HealthTech", "Digital Health", "Health IT", "MedTech", "Telemedicine", "Healthcare Analytics", "Mental Health Tech"],
    "E-Commerce / Retail Tech": ["E-Commerce", "Retail Tech", "D2C", "Direct-to-Consumer", "Online Marketplace", "Social Commerce", "Quick Commerce"],
    "AI / ML / GenAI": ["Artificial Intelligence", "Machine Learning", "Generative AI", "Large Language Models", "MLOps", "Computer Vision", "Natural Language Processing", "AI Infrastructure"],
    "EdTech": ["EdTech", "Education Technology", "Online Learning", "LMS", "E-Learning", "Skill Development"],
    "CleanTech / Climate Tech": ["CleanTech", "Climate Tech", "Renewable Energy", "Energy Tech", "Sustainability", "Carbon Tech", "GreenTech"],
    "Cybersecurity": ["Cybersecurity", "Information Security", "Cloud Security", "Network Security", "Identity Management", "Zero Trust"],
    "InsurTech": ["InsurTech", "Insurance Technology", "Digital Insurance", "Embedded Insurance"],
    "PropTech": ["PropTech", "Real Estate Tech", "Construction Tech", "Smart Buildings"],
    "HR Tech": ["HR Tech", "Human Capital Management", "Talent Management", "Workforce Tech", "Recruiting Tech"],
    "MarTech": ["MarTech", "Marketing Technology", "Ad Tech", "Customer Data Platform", "CDP", "Growth Marketing"],
    "LegalTech": ["LegalTech", "Legal Technology", "Contract Management", "Compliance Tech"],
    "AgriTech": ["AgriTech", "Agriculture Technology", "Precision Farming", "FoodTech"],
    "Logistics / Supply Chain Tech": ["Logistics Tech", "Supply Chain Tech", "Last-Mile Delivery", "Freight Tech", "Warehousing Tech"],
    "Gaming / Interactive Entertainment": ["Gaming", "Game Tech", "Interactive Entertainment", "Esports", "Mobile Gaming"],
    "Biotech / Life Sciences": ["Biotech", "Life Sciences", "Genomics", "Drug Discovery", "CRO", "CDMO"],
    "DeepTech / Hardware": ["DeepTech", "Hardware", "Semiconductor", "IoT", "Robotics", "Drones", "Advanced Manufacturing"],
    "Media / Content Tech": ["Media Tech", "Content Technology", "Streaming", "Digital Media", "Creator Economy", "Podcast Tech"],
    "Crypto / Web3 / Blockchain": ["Crypto", "Web3", "Blockchain", "DeFi", "NFT", "Digital Assets", "Decentralized Finance"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Hero
# ─────────────────────────────────────────────────────────────────────────────
st.title("Lead Operations")
st.markdown("A global, geography- and industry-agnostic lead intelligence engine.")

if "lead_job_dir" not in st.session_state:
    st.session_state.lead_job_dir = ""

def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _save_uploads(files, job_dir: Path) -> list[str]:
    saved: list[str] = []
    upload_dir = job_dir / "dedupe_inputs"
    upload_dir.mkdir(parents=True, exist_ok=True)
    for file in files or []:
        target = upload_dir / Path(file.name).name
        target.write_bytes(file.getbuffer())
        saved.append(str(target.resolve()))
    return saved


# ─────────────────────────────────────────────────────────────────────────────
# Section 01 — Input
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    '<div class="section-rule"><span class="section-index">01 / Input</span><h2>Build the filter contract</h2><span class="micro">Every populated field is enforced</span></div>',
    unsafe_allow_html=True,
)

with st.form("lead_contract", clear_on_submit=False):
    top_a, top_b, top_c, top_d = st.columns([1.1, 1, 1, 1])
    target_count = top_a.number_input(
        "Verified lead target", min_value=1, max_value=2000, value=150
    )
    business_model = top_b.selectbox(
        "Business model",
        ["Any", "B2B only", "B2C only"],
        index=0,
    )
    minimum_confidence = top_c.slider(
        "Minimum confidence", min_value=60, max_value=99, value=85
    )
    require_target_company = top_d.checkbox(
        "Hard company filter", value=True,
        help="When enabled, the parsed current company must match one of the target companies.",
    )

    left, right = st.columns(2)
    roles = left.multiselect(
        "Roles / personas",
        options=_all_roles,
        default=[],
        help="Role family and seniority are both enforced.",
    )
    custom_roles = left.text_input(
        "Custom roles (comma-separated)",
        "",
        placeholder="e.g. Founder, Technical Lead",
    )
    locations = left.multiselect(
        "Locations",
        options=_all_locations,
        default=[],
        help="Use canonical names from the location taxonomy, e.g. Singapore, London, Bengaluru.",
    )
    custom_locations = left.text_input(
        "Custom locations (comma-separated)",
        "",
        placeholder="e.g. Dubai, New York",
    )
    companies_text = right.text_area(
        "Target companies — one per line",
        "",
        height=140,
        placeholder="For instance:\nRazorpay\nPhonePe\nCRED",
        help="Paste a list of companies here.",
    )
    
    # We flatten industry evidence map to get all possible options for the dropdown
    _all_industries = []
    for k, v in _industry_evidence_map.items():
        _all_industries.extend(v)
    _all_industries = sorted(list(set(_all_industries)))

    industries = right.multiselect(
        "Industry keywords (e.g. Fintech, Banking)",
        options=_all_industries,
        default=[],
        help="These keywords will be included in the search query to narrow down leads to the correct industry.",
    )
    custom_industries = right.text_input(
        "Custom industry keywords (comma-separated)",
        "",
        placeholder="e.g. SpaceTech, Quantum Computing",
    )

    with st.expander("Search budget, source policy, and deduplication", expanded=False):
        a, b, c, d = st.columns(4)
        source_options = [
            "google_browser",
            "bing_browser",
            "duckduckgo_browser",
            "ddgs",
        ]
        source_labels = {
            "google_browser": "Google browser · default",
            "bing_browser": "Bing browser · optional",
            "duckduckgo_browser": "DuckDuckGo browser · optional",
            "ddgs": "DDGS · optional library fallback",
        }
        sources = a.multiselect(
            "Public search sources",
            source_options,
            default=list(
                source_defaults.get("sources")
                or ["google_browser"]
            ),
            format_func=lambda value: source_labels[value],
        )
        minimum_sources = b.number_input(
            "Minimum evidence sources", min_value=1, max_value=5, value=1
        )
        max_queries = c.number_input(
            "Maximum Google queries", min_value=1, max_value=300, value=int(source_defaults.get("max_queries") or 40)
        )
        max_results = d.number_input(
            "Results per query", min_value=5, max_value=100, value=int(source_defaults.get("max_results_per_query") or 20)
        )
        e, f, g = st.columns([1, 1, 1])
        max_pages = e.number_input(
            "Pages per query",
            min_value=1,
            max_value=10,
            value=int(source_defaults.get("max_pages_per_query") or 2),
            help="The browser follows separate result offsets and checkpoints every page.",
        )
        pool_multiplier = f.number_input(
            "Candidate pool × target", min_value=1, max_value=12, value=int(source_defaults.get("candidate_pool_multiplier") or 4)
        )
        headful = g.checkbox(
            "Show browser windows",
            value=not bool(source_defaults.get("browser_headless", False)),
            help=(
                "Keeps the persistent Google Chrome search window visible."
            ),
        )
        manual_google_recovery = g.checkbox(
            "Manual Google recovery",
            value=bool(source_defaults.get("google_manual_challenge_seconds", 0)),
            disabled=not headful or "google_browser" not in sources,
            help=(
                "Wait up to 180 seconds when Google requests verification. CAPTCHA images remain "
                "enabled; after you solve it, the same query continues automatically."
            ),
        )
        q1, q2, q3 = st.columns([1, 1, 1])
        query_mode = q1.selectbox(
            "Query strategy",
            ["Balanced", "Exact / strict", "Exploratory"],
            help="Balanced mixes precise company-role searches with wider context queries.",
        )
        include_terms_text = q2.text_area(
            "Required query terms",
            placeholder="e.g. payments\ncustomer experience",
            height=90,
            help="Every term is quoted and added to each search query.",
        )
        exclude_terms_text = q3.text_area(
            "Exclude from queries",
            placeholder="e.g. jobs\nrecruiter",
            height=90,
            help="Terms become negative search clauses, useful for removing hiring noise.",
        )

    st.markdown("##### Exclusion list (optional)")
    h1, h2 = st.columns(2)
    dedupe_files = h1.file_uploader(
        "Upload prior CSV/XLSX exports to exclude",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help=(
            "Upload one or more previous exports. Every sheet in every workbook is scanned "
            "for valid LinkedIn URLs. Any matching leads will be skipped across all searches."
        ),
    )
    gsheet_url = h2.text_input(
        "Or Google Sheet URL to exclude",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        help="Paste a public Google Sheet URL to exclude existing POCs from the search.",
    )
    contract_left, contract_right = st.columns([1.45, 1])
    with contract_left:
        with st.container(key="lead-run-action"):
            submitted = st.form_submit_button(
                "Commit contract & start background job",
                type="primary",
                width="stretch",
            )
    with contract_right:
        st.markdown(
            '<div class="contract"><span class="micro">Accuracy policy</span><p><strong>Role → company → location → industry → business model → source count → confidence.</strong><br>Failure at any selected gate sends the candidate to the rejection ledger with a reason.</p></div>',
            unsafe_allow_html=True,
        )

if submitted:
    # Combine multiselect with custom text inputs
    final_roles = list(roles)
    if custom_roles:
        final_roles.extend([r.strip() for r in custom_roles.split(",") if r.strip()])
        
    final_locations = list(locations)
    if custom_locations:
        final_locations.extend([l.strip() for l in custom_locations.split(",") if l.strip()])
        
    final_industries = list(industries)
    if custom_industries:
        final_industries.extend([i.strip() for i in custom_industries.split(",") if i.strip()])
        
    companies = _lines(companies_text)
    effective_sources = list(sources)
    if any(source in {"ddgs", "duckduckgo_browser"} for source in effective_sources) and "google_browser" not in effective_sources:
        effective_sources = ["google_browser", *effective_sources]
    errors = []
    if not final_roles:
        errors.append("Enter at least one role/persona.")
    if not final_locations and not companies:
        errors.append("Enter at least one location or target company so queries stay focused.")
    if require_target_company and not companies:
        errors.append("Hard company filtering requires at least one target company.")
    if not sources:
        errors.append("Select at least one search source.")
    if int(minimum_sources) > len(independent_source_families(effective_sources)):
        errors.append(
            "Minimum evidence sources cannot exceed the number of independent selected sources."
        )
    if errors:
        for message in errors:
            st.error(message)
    else:
        sources = effective_sources
        config = {
            "preset": preset_name,
            "target_count": int(target_count),
            "business_model": business_model,
            "roles": final_roles,
            "locations": final_locations,
            "industries": final_industries,
            "company_names": companies,
            "sources": sources,
            "max_queries": int(max_queries),
            "max_results_per_query": int(max_results),
            "max_pages_per_query": int(max_pages),
            "source_failure_limit": int(source_defaults.get("source_failure_limit") or 3),
            "candidate_pool_multiplier": int(pool_multiplier),
            "browser_headless": not headful,
            "google_manual_challenge_seconds": (
                180 if headful and manual_google_recovery else 0
            ),
            "require_target_company": require_target_company,
            "minimum_confidence": int(minimum_confidence),
            "minimum_sources": int(minimum_sources),
            "query_mode": query_mode,
            "include_terms": _lines(include_terms_text),
            "exclude_terms": _lines(exclude_terms_text),
            "existing_files": [],
        }
        job_dir = create_job("lead_harvest", config).resolve()
        config["existing_files"] = _save_uploads(dedupe_files, job_dir) + download_gsheet(gsheet_url, job_dir)
        write_json(job_dir / "config.json", config)
        launch_job(job_dir, "speedy_scraper.lead_job")
        st.session_state.lead_job_dir = str(job_dir)
        # Clear builder overrides after job launch
        for _key in ("_roles_override", "_locations_override", "_companies_override", "_industries_override"):
            st.session_state[_key] = None
        st.rerun()

previous_jobs = list_jobs("lead_harvest")
if previous_jobs:
    with st.expander("Job archive / reopen a checkpoint", expanded=False):
        selected_job = st.selectbox(
            "Saved lead job",
            previous_jobs,
            format_func=lambda value: f"{value.name} · {read_status(value).get('state', 'unknown')}",
        )
        col1, col2, col3 = st.columns([1, 1, 1])
        if col1.button("Open selected job"):
            st.session_state.lead_job_dir = str(selected_job.resolve())
            st.rerun()
        if col2.button("❌ Delete selected job"):
            import shutil
            shutil.rmtree(selected_job)
            if st.session_state.lead_job_dir == str(selected_job.resolve()):
                st.session_state.lead_job_dir = ""
            st.rerun()
        if col3.button("🗑️ Clear all jobs"):
            import shutil
            for j in previous_jobs:
                shutil.rmtree(j)
            st.session_state.lead_job_dir = ""
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Section 02 — Operations Ledger
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    '<div class="section-rule"><span class="section-index">02 / Run</span><h2>Operations ledger</h2><span class="micro">Persistent worker telemetry</span></div>',
    unsafe_allow_html=True,
)


def _phase_markup(current: str, state: str) -> str:
    phases = [("search", "Discover"), ("consolidate", "Consolidate"), ("verify", "Verify"), ("export", "Export")]
    effective = "export" if state == "completed" else current
    order = {key: index for index, (key, _name) in enumerate(phases)}
    active_index = order.get(effective, 0)
    cells = []
    for index, (_key, name) in enumerate(phases):
        css = "done" if index < active_index or state == "completed" else ("active" if index == active_index else "")
        cells.append(
            f'<div class="phase {css}"><div class="phase-num">0{index + 1}</div><div class="phase-name">{name}</div></div>'
        )
    return '<div class="phase-grid">' + "".join(cells) + "</div>"


def _worker_log_tail(job_dir: Path, *, limit: int = 5000) -> str:
    try:
        content = (job_dir / "worker.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return content[-limit:].strip()


@st.fragment(run_every="2s")
def job_monitor() -> None:
    raw_path = str(st.session_state.get("lead_job_dir") or "")
    if not raw_path:
        st.info("Commit a filter contract above to create the first persistent lead job.")
        return
    job_dir = Path(raw_path)
    status = read_status(job_dir)
    state = str(status.get("state") or "unknown")
    phase = str(status.get("phase") or "search")
    stale = job_is_stale(status)
    heartbeat_age = heartbeat_age_seconds(status)
    result, checkpoint = load_lead_job_checkpoint(job_dir)
    job_config = read_json(job_dir / "config.json", default={})
    if not isinstance(job_config, dict):
        job_config = {}

    st.markdown(_phase_markup(phase, state), unsafe_allow_html=True)
    active = state in {"starting", "running", "stopping"}
    live = active and not stale and heartbeat_age is not None and heartbeat_age <= 8
    dot_class = "" if live else ("stale" if active else "done")
    current_detail = " · ".join(
        str(value)
        for value in (
            status.get("current_source"),
            f"page {status.get('current_page')}" if status.get("current_page") else "",
            status.get("current_name"),
            status.get("current_company"),
            status.get("activity"),
        )
        if value
    ) or str(status.get("message") or "Worker ready")
    label = "WORKER LIVE" if live else ("WORKER RECOVERY NEEDED" if stale else state.upper())
    st.markdown(
        f'<div class="live-strip"><span class="live-dot {dot_class}"></span><div><span class="micro">{html.escape(label)}</span><br><span style="font-size:11px;color:#68675f">{html.escape(current_detail)}</span></div></div>',
        unsafe_allow_html=True,
    )
    if stale:
        st.warning("This worker is no longer alive. Relaunching continues from its last atomic checkpoint.")
    if state == "failed" or stale:
        worker_log = _worker_log_tail(job_dir)
        if worker_log:
            with st.expander("Worker diagnostics", expanded=state == "failed"):
                st.code(worker_log, language="text")

    captcha_recovery_panel(
        status,
        job_dir=job_dir,
        module="speedy_scraper.lead_job",
        button_key="lead_captcha_recovery",
        launch_job=launch_job,
        request_stop=request_stop,
        read_status=read_status,
    )

    processed = int(status.get("processed") or 0)
    total = int(status.get("total") or 0)
    if total:
        st.progress(min(processed / total, 1.0), text=f"{phase.upper()} · {processed}/{total}")

    controls_left, controls_right, controls_meta = st.columns([1, 1, 2])
    if active and not stale:
        with controls_left.container(key="lead-stop-action"):
            if st.button(
                "Stop after current unit",
                disabled=state == "stopping",
                width="stretch",
            ):
                request_stop(job_dir)
                st.rerun(scope="fragment")
    elif state in {"paused", "failed"} or stale:
        with controls_left.container(key="lead-resume-action"):
            if st.button("Relaunch from checkpoint", width="stretch"):
                launch_job(job_dir, "speedy_scraper.lead_job")
                st.rerun(scope="fragment")
    with controls_right.container(key="lead-refresh-action"):
        st.button("Refresh ledger", width="stretch")
    controls_meta.caption(
        f"JOB {job_dir.name} · updated {datetime.now().strftime('%H:%M:%S')} · checkpoint v{checkpoint.get('version', '—')}"
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Candidates", int(status.get("candidates") or result.metrics.get("candidates_found", 0)))
    m2.metric("Verified", len(result.leads))
    m3.metric("Rejected", len(result.rejections))
    m4.metric("Duplicates", int(result.metrics.get("duplicates", 0)))
    m5.metric("Source errors", len(result.source_errors))

    verified_tab, rejection_tab, contract_tab, source_tab = st.tabs(
        ["Verified leads", "Rejection ledger", "Filter contract", "Source health"]
    )
    with verified_tab:
        if result.leads:
            st.dataframe(leads_frame(result.leads), width="stretch", hide_index=True)
        else:
            st.caption("No candidates have cleared every active gate yet.")
    with rejection_tab:
        if result.rejections:
            st.dataframe(rejections_frame(result.rejections), width="stretch", hide_index=True)
        else:
            st.caption("Rejected candidates will appear here with an exact reason code.")
    with contract_tab:
        summary = {
            "Preset": job_config.get("preset") or "—",
            "Roles": ", ".join(job_config.get("roles") or []),
            "Locations": ", ".join(job_config.get("locations") or []) or "Not enforced",
            "Companies": ", ".join(job_config.get("company_names") or []) or "Not enforced",
            "Company mode": "Hard filter" if job_config.get("require_target_company") else "Discovery context",
            "Industries": ", ".join(job_config.get("industries") or []) or "Not enforced",
            "Business model": job_config.get("business_model") or "Any",
            "Minimum confidence": job_config.get("minimum_confidence") or 0,
            "Minimum sources": job_config.get("minimum_sources") or 1,
            "Sources": ", ".join(job_config.get("sources") or []),
            "Query budget": job_config.get("max_queries") or "default",
            "Results / query": job_config.get("max_results_per_query") or "default",
            "Pages / query": job_config.get("max_pages_per_query") or "default",
            "Query strategy": job_config.get("query_mode") or "Balanced",
            "Required terms": ", ".join(job_config.get("include_terms") or []) or "None",
            "Excluded terms": ", ".join(job_config.get("exclude_terms") or []) or "None",
        }
        st.dataframe(
            [{"Filter": key, "Committed value": str(value)} for key, value in summary.items()],
            width="stretch",
            hide_index=True,
        )
    with source_tab:
        if result.source_errors:
            for error in result.source_errors:
                st.error(error)
        else:
            st.success("No source errors recorded for this job.")
        with st.expander(f"Generated query plan ({len(result.queries)})"):
            st.code("\n".join(result.queries), language=None)

    csv_path = Path(str(status.get("csv_path") or ""))
    xlsx_path = Path(str(status.get("xlsx_path") or ""))
    if csv_path.is_file() and xlsx_path.is_file():
        download_a, download_b = st.columns(2)
        download_a.download_button(
            "Download verified CSV",
            csv_path.read_bytes(),
            csv_path.name,
            "text/csv",
            width="stretch",
        )
        download_b.download_button(
            "Download audit workbook",
            xlsx_path.read_bytes(),
            xlsx_path.name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


job_monitor()
