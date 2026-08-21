from __future__ import annotations

import html
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from speedy_scraper.background_jobs import (
    clear_saved_jobs,
    create_job,
    delete_saved_job,
    heartbeat_age_seconds,
    job_is_stale,
    launch_job,
    list_jobs,
    process_is_running,
    read_json,
    read_status,
    request_stop,
    write_json,
)
from speedy_scraper.config import config_from_mapping, load_catalog
from speedy_scraper.exports import leads_frame, rejections_frame
from speedy_scraper.lead_job import load_lead_job_checkpoint
from speedy_scraper.pipeline import load_existing_urls
from speedy_scraper.query import build_queries
from speedy_scraper.sources import independent_source_families
from speedy_scraper.taxonomy import load_location_taxonomy, load_role_taxonomy
from speedy_scraper.ui import (
    action_button_css,
    download_gsheet,
    prepare_failed_search_retry,
)

st.set_page_config(
    page_title="Signal · Public Lead Finder",
    page_icon="S",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(action_button_css(), unsafe_allow_html=True)
st.markdown(
    """
    <style>
    :root {
        --ink:#18211f;
        --muted:#65706d;
        --paper:#f4f1e8;
        --card:#fffdf7;
        --line:#d8d3c6;
        --green:#087d62;
        --green-soft:#dcebe4;
    }
    [data-testid="stAppViewContainer"] {
        background:
            linear-gradient(rgba(24,33,31,.028) 1px, transparent 1px),
            linear-gradient(90deg, rgba(24,33,31,.028) 1px, transparent 1px),
            var(--paper);
        background-size:32px 32px;
        color:var(--ink);
    }
    [data-testid="stHeader"] { background:rgba(244,241,232,.92); }
    [data-testid="stMainBlockContainer"] { max-width:1180px; padding-top:2.2rem; }
    h1,h2,h3 { color:var(--ink); font-family:"Avenir Next",Avenir,sans-serif; }
    p,label,[data-testid="stCaptionContainer"] {
        font-family:"Avenir Next",Avenir,sans-serif;
    }
    .signal-hero {
        position:relative;
        overflow:hidden;
        border:1px solid var(--ink);
        border-radius:2px;
        background:var(--card);
        padding:34px 38px 30px;
        margin-bottom:20px;
        box-shadow:7px 7px 0 var(--ink);
    }
    .signal-hero:after {
        content:"G";
        position:absolute;
        right:18px;
        top:-34px;
        color:rgba(8,125,98,.10);
        font:800 150px/1 Georgia,serif;
    }
    .eyebrow,.micro {
        color:var(--green);
        font-size:11px;
        font-weight:800;
        letter-spacing:.14em;
        text-transform:uppercase;
    }
    .signal-hero h1 {
        max-width:760px;
        margin:8px 0 6px;
        font:700 clamp(35px,5vw,62px)/.98 Georgia,serif;
        letter-spacing:-.045em;
    }
    .signal-hero p { max-width:680px; color:var(--muted); margin:12px 0 0; }
    [data-testid="stForm"] {
        background:rgba(255,253,247,.94);
        border:1px solid var(--line);
        border-radius:2px;
        padding:24px 26px 22px;
    }
    [data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] {
        background:#fff!important;
        border-radius:2px!important;
    }
    .google-note {
        display:flex;
        gap:12px;
        align-items:flex-start;
        background:var(--green-soft);
        border-left:4px solid var(--green);
        padding:14px 16px;
        margin:2px 0 18px;
        font-size:14px;
    }
    .dedupe-note {
        color:var(--muted);
        font-size:13px;
        margin:-2px 0 8px;
    }
    .status-card {
        display:flex;
        justify-content:space-between;
        gap:24px;
        align-items:center;
        background:var(--ink);
        color:#fff;
        padding:18px 22px;
        border-radius:2px;
        margin:12px 0 14px;
    }
    .status-card strong { font-size:18px; }
    .status-card small { color:#bdc8c4; }
    .status-dot {
        display:inline-block;
        width:9px;
        height:9px;
        margin-right:9px;
        border-radius:50%;
        background:#67ddb8;
        box-shadow:0 0 0 5px rgba(103,221,184,.12);
    }
    .status-dot.idle { background:#d9a740; box-shadow:none; }
    [data-testid="stMetric"] {
        background:rgba(255,253,247,.88);
        border-top:3px solid var(--ink);
        padding:14px 16px;
    }
    [data-testid="stMetricValue"] { font-family:Georgia,serif; }
    .section-head {
        display:flex;
        justify-content:space-between;
        align-items:end;
        border-bottom:1px solid var(--ink);
        margin:30px 0 14px;
        padding-bottom:8px;
    }
    .section-head h2 { margin:0; font:700 25px/1 Georgia,serif; }
    .stAlert { border-radius:2px; }
    footer { visibility:hidden; }
    @media (max-width:700px) {
        .signal-hero { padding:26px 22px; box-shadow:4px 4px 0 var(--ink); }
        .status-card { align-items:flex-start; flex-direction:column; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _items(value: str) -> list[str]:
    """Accept comma-separated or line-separated filters and keep their order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in value.replace(",", "\n").splitlines():
        item = " ".join(raw.split())
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _search_filter_options() -> tuple[list[str], list[str], list[str]]:
    """Return the canonical dropdown choices used by the lead-search brief."""
    role_options = list(load_role_taxonomy().keys())
    location_options = list(load_location_taxonomy().keys())

    industry_values: set[str] = set()
    catalog = load_catalog()
    for preset in (catalog.get("presets") or {}).values():
        if isinstance(preset, dict):
            industry_values.update(
                str(value).strip()
                for value in preset.get("industries") or []
                if str(value).strip()
            )
    return role_options, location_options, sorted(industry_values, key=str.casefold)


def _save_uploads(files: list[object] | None, job_dir: Path) -> list[str]:
    """Persist user-supplied prior exports inside the job's audit directory."""
    saved: list[str] = []
    upload_dir = job_dir / "dedupe_inputs"
    upload_dir.mkdir(parents=True, exist_ok=True)
    for uploaded in files or []:
        target = upload_dir / Path(str(uploaded.name)).name
        target.write_bytes(uploaded.getbuffer())
        saved.append(str(target.resolve()))
    return saved


def _worker_log_tail(job_dir: Path, *, limit: int = 5000) -> str:
    try:
        return (job_dir / "worker.log").read_text(
            encoding="utf-8", errors="replace"
        )[-limit:].strip()
    except OSError:
        return ""


def _prepare_saved_provider_retry(job_dir: Path) -> None:
    """Retry failed cursors without replacing the engines selected by the user."""
    config = read_json(job_dir / "config.json", default={})
    config = config if isinstance(config, dict) else {}
    config["retry_failed_searches"] = True
    write_json(job_dir / "config.json", config)


def _unreviewed_candidates_frame(checkpoint: dict, result) -> pd.DataFrame:
    """Expose candidates preserved before a failed or paused verification pass."""
    reviewed_urls = {
        item.linkedin_url for item in [*result.leads, *result.rejections]
    }
    rows = []
    for item in checkpoint.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("linkedin_url") or "")
        if not url or url in reviewed_urls:
            continue
        rows.append(
            {
                "Name": str(item.get("name") or ""),
                "Designation": str(item.get("designation") or ""),
                "Company": str(item.get("company") or ""),
                "LinkedIn URL": url,
                "Status": "Awaiting verification",
                "Search Evidence": str(item.get("evidence") or ""),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "Name",
            "Designation",
            "Company",
            "LinkedIn URL",
            "Status",
            "Search Evidence",
        ],
    )


st.markdown(
    """
    <div class="signal-hero">
      <div class="eyebrow">Signal / multi-engine lead finder</div>
      <h1>Search less.<br>Find the right people.</h1>
      <p>Run focused public searches through Google, DDGS, or DuckDuckGo and keep only personal LinkedIn profiles that pass every selected filter.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

previous_jobs = list_jobs("lead_harvest")
if "lead_job_dir" not in st.session_state:
    st.session_state.lead_job_dir = str(previous_jobs[0].resolve()) if previous_jobs else ""

st.markdown(
    '<div class="section-head"><h2>Search brief</h2><span class="micro">Only the filters that matter</span></div>',
    unsafe_allow_html=True,
)
st.markdown(
    """
    <div class="google-note"><strong>Choose the search engines.</strong><span>DDGS runs without a browser. Browser providers can stay visible so you can complete a verification prompt if one appears.</span></div>
    """,
    unsafe_allow_html=True,
)

with st.form("public_lead_search", clear_on_submit=False):
    left, right = st.columns(2, gap="large")
    role_options, location_options, industry_options = _search_filter_options()
    selected_roles = left.multiselect(
        "Who do you want to find?",
        options=role_options,
        placeholder="Choose roles or type a custom title",
        accept_new_options=True,
        help="Choose one or more canonical roles, or type a custom title and press Enter.",
    )
    companies_text = right.text_area(
        "Companies (optional)",
        height=112,
        placeholder="Razorpay\nPhonePe\nCRED",
        help="Leave blank to discover people across all matching companies.",
    )
    selected_locations = left.multiselect(
        "Locations",
        options=location_options,
        placeholder="Choose locations or type a custom place",
        accept_new_options=True,
        help="Candidates must show one of these locations in their public result card. Type a custom place and press Enter if it is not listed.",
    )
    selected_industries = right.multiselect(
        "Industry keywords (optional)",
        options=industry_options,
        placeholder="Choose industries or type a keyword",
        accept_new_options=True,
        help="Choose from the catalog or type a custom keyword and press Enter.",
    )

    source_labels = {
        "google_browser": "Google browser",
        "ddgs": "DDGS metasearch",
        "duckduckgo_browser": "DuckDuckGo browser",
        "bing_browser": "Bing browser",
    }
    search_sources = st.multiselect(
        "Public search engines",
        options=list(source_labels),
        default=["google_browser", "ddgs"],
        format_func=lambda value: source_labels[value],
        help="Each selected engine runs independently. Google is no longer added automatically.",
    )

    settings_left, settings_middle, settings_right = st.columns([1, 1, 1.5])
    target_count = settings_left.number_input(
        "Lead target", min_value=1, max_value=500, value=50, step=10,
        help="The final count depends on how many public results pass every filter.",
    )
    search_pages = settings_middle.select_slider(
        "Depth / query",
        options=[1, 2, 3, 4, 5],
        value=2,
        help="Each step requests up to 10 more results per query and selected engine.",
    )
    strict_company = settings_right.checkbox(
        "Current company must match",
        value=True,
        help="Applied only when company names are entered.",
    )

    browser_selected = any(source.endswith("_browser") for source in search_sources)
    browser_left, browser_right = st.columns(2)
    show_browser = browser_left.checkbox(
        "Show browser windows",
        value=True,
        disabled=not browser_selected,
        help="Useful for completing Google or DuckDuckGo verification prompts locally.",
    )
    corroborate = browser_right.checkbox(
        "Require two-engine confirmation",
        value=False,
        disabled=len(independent_source_families(search_sources)) < 2,
        help="Improves precision but returns only profiles found by two selected engines.",
    )

    with st.expander("Accuracy rules", expanded=False):
        accuracy_left, accuracy_right = st.columns(2, gap="large")
        include_terms_text = accuracy_left.text_area(
            "Required evidence terms — one per line",
            height=90,
            placeholder="B2B\npayments",
            help="Every term must appear in the candidate's own result evidence.",
        )
        exclude_terms_text = accuracy_right.text_area(
            "Reject evidence terms — one per line",
            height=90,
            placeholder="former\nconsultant",
            help="A candidate is rejected if any term appears in their result evidence.",
        )

    with st.expander("Remove duplicates", expanded=False):
        st.markdown(
            '<div class="dedupe-note">Add previous exports or a public Google Sheet. Every personal LinkedIn URL already present will be skipped.</div>',
            unsafe_allow_html=True,
        )
        dedupe_left, dedupe_right = st.columns(2, gap="large")
        dedupe_files = dedupe_left.file_uploader(
            "Upload previous CSV / Excel files",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            help="Every sheet and every column is scanned for LinkedIn profile URLs.",
        )
        gsheet_url = dedupe_right.text_input(
            "Public Google Sheet link",
            placeholder="https://docs.google.com/spreadsheets/d/.../edit#gid=0",
            help="The tab identified by gid must be publicly readable.",
        )

    with st.container(key="lead-run-action"):
        submitted = st.form_submit_button(
            "Find verified leads",
            type="primary",
            width="stretch",
        )

if submitted:
    roles = _items("\n".join(str(item) for item in selected_roles))
    locations = _items("\n".join(str(item) for item in selected_locations))
    companies = _items(companies_text)
    industries = _items("\n".join(str(item) for item in selected_industries))
    include_terms = _items(include_terms_text)
    exclude_terms = _items(exclude_terms_text)
    errors: list[str] = []
    if not roles:
        errors.append("Add at least one job title.")
    if not locations and not companies:
        errors.append("Add a location or at least one company to keep searches focused.")
    if not search_sources:
        errors.append("Select at least one public search engine.")
    non_industry_terms = [
        industry
        for industry in industries
        if industry.casefold() in {"hiring", "recruiter", "recruiting", "recruitment"}
    ]
    if non_industry_terms:
        errors.append(
            f"{', '.join(non_industry_terms)} is not an industry. Remove it from Industry "
            "keywords; use Required evidence terms only when that wording must appear."
        )
    if errors:
        for message in errors:
            st.error(message)
    else:
        # Scale larger targets while keeping the total provider-request plan bounded.
        max_search_requests = 150
        provider_count = max(1, len(search_sources))
        max_query_budget = max_search_requests // (int(search_pages) * provider_count)
        automatic_query_budget = min(
            max_query_budget,
            max(12, math.ceil(int(target_count) / (int(search_pages) * 2.5))),
        )
        config = {
            "ui_version": "multi_source_v2",
            "target_count": int(target_count),
            "business_model": "Any",
            "roles": roles,
            "locations": locations,
            "industries": industries,
            "company_names": companies,
            "sources": search_sources,
            "max_queries": automatic_query_budget,
            "max_results_per_query": int(search_pages) * 10,
            "max_pages_per_query": int(search_pages),
            "source_failure_limit": 2,
            "candidate_pool_multiplier": 2,
            "browser_headless": not bool(show_browser and browser_selected),
            "google_manual_challenge_seconds": (
                60 if show_browser and "google_browser" in search_sources else 0
            ),
            "require_target_company": bool(companies and strict_company),
            "minimum_confidence": 80,
            "minimum_sources": 2 if corroborate else 1,
            "query_mode": "Strict",
            "include_terms": include_terms,
            "exclude_terms": exclude_terms,
            "existing_files": [],
        }
        full_plan_config = {**config, "max_queries": max_query_budget}
        full_plan_size = len(build_queries(config_from_mapping(full_plan_config)))
        config["max_queries"] = min(
            max_query_budget,
            max(automatic_query_budget, full_plan_size),
        )
        job_dir = create_job("lead_harvest", config).resolve()
        sheet_files = download_gsheet(gsheet_url, job_dir)
        existing_files = _save_uploads(dedupe_files, job_dir) + sheet_files
        config["existing_files"] = existing_files
        config["existing_url_count"] = len(
            load_existing_urls([Path(value) for value in existing_files])
        )
        write_json(job_dir / "config.json", config)
        if gsheet_url.strip() and not sheet_files:
            st.warning(
                "The search was not started because the Google Sheet could not be loaded. "
                "Make the selected tab public or remove the link, then submit again."
            )
        else:
            launch_job(job_dir, "speedy_scraper.lead_job")
            st.session_state.lead_job_dir = str(job_dir)
            st.rerun()

previous_jobs = list_jobs("lead_harvest")
if previous_jobs:
    with st.expander("Open an earlier run", expanded=False):
        selected_job = st.selectbox(
            "Saved run",
            previous_jobs,
            format_func=lambda value: (
                f"{value.name} · {read_status(value).get('state', 'unknown')}"
            ),
            label_visibility="collapsed",
        )
        selected_status = read_status(selected_job)
        selected_live = (
            str(selected_status.get("state") or "") in {"starting", "running", "stopping"}
            and process_is_running(int(selected_status.get("pid") or 0))
        )
        archive_open, archive_delete, archive_clear = st.columns([1, 1, 1])
        if archive_open.button("Open selected", width="stretch"):
            st.session_state.lead_job_dir = str(selected_job.resolve())
            st.rerun()
        with archive_delete.container(key="lead-delete-job-action"):
            if st.button(
                "✕ Delete selected",
                disabled=selected_live,
                help="Stop a live worker before deleting its files.",
                width="stretch",
            ):
                st.session_state.lead_delete_job = str(selected_job.resolve())
                st.rerun()
        with archive_clear.container(key="lead-clear-jobs-action"):
            if st.button("Clear saved runs", width="stretch"):
                st.session_state.lead_confirm_clear_jobs = True
                st.rerun()

        delete_target = st.session_state.get("lead_delete_job")
        if delete_target:
            delete_path = Path(str(delete_target)).resolve()
            st.warning(
                f"Delete **{delete_path.name}** including its checkpoints, logs, and exports?"
            )
            confirm_delete, cancel_delete = st.columns(2)
            with confirm_delete.container(key="lead-confirm-delete-action"):
                if st.button("Confirm delete", type="primary", width="stretch"):
                    removed = delete_saved_job(delete_path, "lead_harvest")
                    if removed and st.session_state.get("lead_job_dir") == str(delete_path):
                        st.session_state.lead_job_dir = ""
                    st.session_state.lead_archive_message = (
                        f"Deleted {delete_path.name}."
                        if removed
                        else "That run is still live and was kept. Stop it before deleting."
                    )
                    st.session_state.pop("lead_delete_job", None)
                    st.rerun()
            if cancel_delete.button("Cancel", width="stretch"):
                st.session_state.pop("lead_delete_job", None)
                st.rerun()

        if st.session_state.get("lead_confirm_clear_jobs"):
            st.warning(
                "Delete every saved Lead Finder run, including its checkpoints, logs, and "
                "exports? Workers that are still live will be kept."
            )
            confirm_clear, cancel_clear = st.columns(2)
            with confirm_clear.container(key="lead-confirm-clear-action"):
                if st.button("Confirm clear saved runs", type="primary", width="stretch"):
                    deleted, kept = clear_saved_jobs("lead_harvest")
                    current_job = str(st.session_state.get("lead_job_dir") or "")
                    if current_job and Path(current_job).name in deleted:
                        st.session_state.lead_job_dir = ""
                    message = f"Deleted {len(deleted)} saved run(s)."
                    if kept:
                        message += f" Kept {len(kept)} live run(s)."
                    st.session_state.lead_archive_message = message
                    st.session_state.pop("lead_confirm_clear_jobs", None)
                    st.rerun()
            if cancel_clear.button("Cancel", width="stretch"):
                st.session_state.pop("lead_confirm_clear_jobs", None)
                st.rerun()

archive_message = st.session_state.pop("lead_archive_message", None)
if archive_message:
    st.success(str(archive_message))

st.markdown(
    '<div class="section-head"><h2>Live results</h2><span class="micro">Checkpointed after every provider page</span></div>',
    unsafe_allow_html=True,
)


@st.fragment(run_every="2s")
def job_monitor() -> None:
    raw_path = str(st.session_state.get("lead_job_dir") or "")
    if not raw_path:
        st.info("Enter a search brief above to start your first public lead run.")
        return

    job_dir = Path(raw_path)
    status = read_status(job_dir)
    state = str(status.get("state") or "unknown")
    phase = str(status.get("phase") or "search")
    stale = job_is_stale(status)
    result, checkpoint = load_lead_job_checkpoint(job_dir)
    job_config = read_json(job_dir / "config.json", default={})
    if not isinstance(job_config, dict):
        job_config = {}

    active = state in {"starting", "running", "stopping"}
    heartbeat_age = heartbeat_age_seconds(status)
    live = active and not stale and (heartbeat_age is None or heartbeat_age <= 8)
    configured_sources = [str(item) for item in job_config.get("sources") or []]
    current_source = str(status.get("current_source") or "")
    current_source_label = source_labels.get(current_source, current_source or "search provider")
    detail = str(status.get("message") or "Preparing search providers")
    current_page = status.get("current_page")
    if current_page:
        detail = f"{current_source_label} · page {current_page} · {detail}"
    label = "PUBLIC SEARCH RUNNING" if live else (
        "RECOVERY NEEDED" if stale else state.replace("_", " ").upper()
    )
    st.markdown(
        f"""
        <div class="status-card">
          <div><span class="status-dot {'idle' if not live else ''}"></span><strong>{html.escape(label)}</strong></div>
          <small>{html.escape(detail)}</small>
        </div>
        """,
        unsafe_allow_html=True,
    )

    processed = int(status.get("processed") or 0)
    total = int(status.get("total") or 0)
    if active and total:
        st.progress(
            min(processed / total, 1.0),
            text=f"{phase.title()} · {processed} of {total}",
        )

    errors = [str(item) for item in result.source_errors]
    challenged = bool(status.get("captcha_required")) or any(
        "challenge" in item.lower() or "unusual traffic" in item.lower()
        for item in errors
    )
    challenge_source = str(status.get("captcha_source") or "")
    ip_changed = any("public ip changed" in item.lower() for item in errors)
    recovery_action_rendered = False
    if ip_changed:
        st.error(
            "Google detected two public IP addresses in this browser session. Turn off your "
            "VPN/proxy or iCloud Private Relay, keep one network connection active, then retry. "
            "This CAPTCHA cannot be completed reliably while the IP is changing."
        )
        if not active:
            recovery_action_rendered = True
            with st.container(key="lead-network-retry-action"):
                if st.button("Retry after fixing the network", width="stretch"):
                    prepare_failed_search_retry(job_dir, local_manual=True)
                    request_stop(job_dir)
                    launch_job(job_dir, "speedy_scraper.lead_job")
                    st.rerun(scope="fragment")
    elif challenged and active:
        st.warning(
            f"{source_labels.get(challenge_source, challenge_source or 'A search provider')} "
            "needs verification. Complete it in the open browser window if available; "
            "the other selected providers continue independently."
        )
    elif challenged:
        st.error(f"{source_labels.get(challenge_source, challenge_source or 'A search provider')} did not release its verification page in time.")
        recovery_action_rendered = True
        with st.container(key="lead-challenge-retry-action"):
            google_recovery = challenge_source == "google_browser" or (
                not challenge_source and "google_browser" in configured_sources
            )
            retry_label = (
                "Retry the failed Google page in Chrome"
                if google_recovery
                else "Retry failed searches with selected engines"
            )
            if st.button(retry_label, width="stretch"):
                if google_recovery:
                    prepare_failed_search_retry(job_dir, local_manual=True)
                else:
                    _prepare_saved_provider_retry(job_dir)
                request_stop(job_dir)
                launch_job(job_dir, "speedy_scraper.lead_job")
                st.rerun(scope="fragment")

    if stale:
        st.warning("The worker stopped responding. Its last completed provider page is saved.")

    collected_count = len(checkpoint.get("candidates") or [])
    reviewed_count = len(result.leads) + len(result.rejections)
    can_verify_collected = (
        not active and phase == "search" and collected_count > reviewed_count
    )
    controls_a, controls_b, controls_c, controls_meta = st.columns([1, 1, 1, 2])
    if active and not stale:
        with controls_a.container(key="lead-stop-action"):
            if st.button("Stop safely", disabled=state == "stopping", width="stretch"):
                request_stop(job_dir)
                st.rerun(scope="fragment")
    elif (state in {"paused", "failed"} or stale) and not recovery_action_rendered:
        with controls_a.container(key="lead-resume-action"):
            if st.button("Resume selected engines", width="stretch"):
                launch_job(job_dir, "speedy_scraper.lead_job")
                st.rerun(scope="fragment")
    if can_verify_collected:
        with controls_b.container(key="lead-verify-collected-action"):
            if st.button(
                f"Verify {collected_count - reviewed_count} collected",
                width="stretch",
            ):
                verify_config = read_json(job_dir / "config.json", default={})
                verify_config = verify_config if isinstance(verify_config, dict) else {}
                verify_config["verify_collected_only"] = True
                write_json(job_dir / "config.json", verify_config)
                launch_job(job_dir, "speedy_scraper.lead_job")
                st.rerun(scope="fragment")
    with controls_c.container(key="lead-refresh-action"):
        st.button("Refresh", width="stretch")
    controls_meta.caption(
        f"Run {job_dir.name} · {datetime.now().strftime('%H:%M:%S')} · "
        f"{len(configured_sources) or 1} engine(s) · checkpoint v{checkpoint.get('version', '—')}"
    )

    metric_a, metric_b, metric_c, metric_d = st.columns(4)
    metric_a.metric(
        "Search candidates",
        int(status.get("candidates") or result.metrics.get("candidates_found", 0)),
    )
    metric_b.metric("Verified leads", len(result.leads))
    metric_c.metric("Filtered out", len(result.rejections))
    metric_d.metric("Duplicates skipped", int(result.metrics.get("duplicates", 0)))

    existing_url_count = int(job_config.get("existing_url_count") or 0)
    if existing_url_count:
        st.caption(
            f"Checking every result against {existing_url_count:,} prior LinkedIn URLs."
        )

    if result.queries:
        with st.expander(f"Focused query plan ({len(result.queries)})", expanded=False):
            st.caption("Every query includes the selected role, location, industry, and evidence rules that apply to it.")
            st.code("\n".join(result.queries), language="text")

    if result.leads:
        st.dataframe(leads_frame(result.leads), width="stretch", hide_index=True)
    elif not active:
        st.info("No public result has passed every selected filter yet.")
    else:
        st.caption("Verified leads will appear here while the selected engines search.")

    if result.rejections:
        rejected_frame = rejections_frame(result.rejections)
        with st.expander(
            f"Rejected candidates ({len(result.rejections)})",
            expanded=not result.leads,
        ):
            st.caption("Each row includes the exact filter reason and search evidence.")
            st.dataframe(rejected_frame, width="stretch", hide_index=True)
            st.download_button(
                "Download rejected candidates (CSV)",
                rejected_frame.to_csv(index=False).encode("utf-8"),
                f"Rejected_Candidates_{job_dir.name}.csv",
                "text/csv",
                width="stretch",
            )

    unreviewed_frame = _unreviewed_candidates_frame(checkpoint, result)
    if not unreviewed_frame.empty:
        with st.expander(
            f"Awaiting verification ({len(unreviewed_frame)})",
            expanded=state in {"failed", "paused"},
        ):
            st.caption(
                "Public search found these profiles, but the run stopped before the accuracy filters "
                "could classify them as verified or rejected."
            )
            st.dataframe(unreviewed_frame, width="stretch", hide_index=True)
            st.download_button(
                "Download unreviewed candidates (CSV)",
                unreviewed_frame.to_csv(index=False).encode("utf-8"),
                f"Unreviewed_Candidates_{job_dir.name}.csv",
                "text/csv",
                width="stretch",
            )

    csv_path = Path(str(status.get("csv_path") or ""))
    xlsx_path = Path(str(status.get("xlsx_path") or ""))
    if csv_path.is_file() and xlsx_path.is_file():
        download_a, download_b = st.columns(2)
        download_a.download_button(
            "Download leads (CSV)",
            csv_path.read_bytes(),
            csv_path.name,
            "text/csv",
            width="stretch",
        )
        download_b.download_button(
            "Download audit (Excel)",
            xlsx_path.read_bytes(),
            xlsx_path.name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    if state == "failed":
        worker_log = _worker_log_tail(job_dir)
        if worker_log:
            with st.expander("Technical details"):
                st.code(worker_log, language="text")


job_monitor()
