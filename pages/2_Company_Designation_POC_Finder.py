from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import streamlit as st
import yaml

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
from speedy_scraper.company_pocs import (
    company_poc_review_frame,
    company_pocs_frame,
    load_company_poc_checkpoint,
)
from speedy_scraper.models import DEFAULT_SOURCE_NAMES
from speedy_scraper.ui import (
    action_button_css,
    captcha_recovery_panel,
    download_gsheet,
    is_streamlit_cloud,
)

_config_dir = Path(__file__).parent.parent / "config"

def _load_yaml(name: str) -> dict:
    path = _config_dir / name
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}

_location_tax: dict = _load_yaml("location_taxonomy.yaml").get("locations", {})
_role_tax: dict = _load_yaml("role_taxonomy.yaml").get("roles", {})

_all_locations = list(_location_tax.keys())
_all_roles = list(_role_tax.keys())

st.set_page_config(page_title="Company + Designation POC Finder", layout="wide")
cloud_runtime = is_streamlit_cloud()
st.markdown(action_button_css(), unsafe_allow_html=True)
# CSS removed to rely on Streamlit default light theme

st.title("Company + Designation POC Finder")
st.markdown(
    "Target the exact companies and roles you care about. Speedy Scraper automatically searches public results "
    "for personal LinkedIn profiles and verifies both inputs against candidate records."
)

with st.form("company_poc_form"):
    left, mid, right = st.columns(3)
    companies_text = left.text_area(
        "Company names — one per line", "", height=180
    )
    designations = mid.multiselect(
        "Designations",
        options=_all_roles,
        default=[],
        placeholder="Select designations...",
    )
    locations = right.multiselect(
        "Locations (optional)",
        options=_all_locations,
        default=[],
        placeholder="Select locations...",
    )
    target_count = st.number_input("Maximum matched POCs", min_value=1, max_value=1000, value=150)
    retry_attempts = st.number_input(
        "Automatic fallback retries per failed search",
        min_value=1,
        max_value=5,
        value=2,
        help="Each failed provider search gets targeted fallback attempts before warning completion.",
    )
    sources = st.multiselect(
        "Public search sources",
        ["google_browser", "ddgs", "bing_browser", "duckduckgo_browser"],
        default=["google_browser", "ddgs"],
    )
    headful = st.checkbox(
        "Show browser windows (local only)",
        value=False,
        disabled=cloud_runtime,
        help="Streamlit Cloud workers cannot expose a remote Chrome window to your computer.",
    )
    manual_google_recovery = st.checkbox(
        "Manual Google recovery (wait up to 180 seconds)",
        value=False,
        disabled=cloud_runtime or not headful or "google_browser" not in sources,
        help="Off by default: Google challenges fail fast and the other sources continue.",
    )
    if cloud_runtime:
        st.caption("Hosted mode: provider fallback and retry are automatic; manual Chrome recovery is local-only.")
    include_terms_text = st.text_area(
        "Required search terms — one per line",
        placeholder="e.g. payments\ncustomer experience",
        height=80,
        help="Adds quoted context to every company/designation query.",
    )
    exclude_terms_text = st.text_area(
        "Exclude search terms — one per line",
        placeholder="e.g. jobs\nrecruiter",
        height=80,
        help="Adds negative clauses to reduce hiring and directory noise.",
    )
    existing_files = st.file_uploader(
        "Prior POC/speaker exports to exclude",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help="Existing LinkedIn URLs and name/company identities will not be returned again.",
    )
    gsheet_url = st.text_input(
        "Or Google Sheet URL to exclude",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        help="Paste a public Google Sheet URL to exclude existing POCs from the search.",
    )
    with st.container(key="company-run-action"):
        start = st.form_submit_button(
            "Start Company POC Job",
            type="primary",
            width="stretch",
        )

if "company_poc_job_dir" not in st.session_state:
    st.session_state.company_poc_job_dir = ""


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _save_uploads(files, job_dir: Path) -> list[str]:
    upload_dir = job_dir / "dedupe_inputs"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for file in files or []:
        target = upload_dir / Path(file.name).name
        target.write_bytes(file.getbuffer())
        saved.append(str(target.resolve()))
    return saved


if start:
    companies = _lines(companies_text)
    if not companies or not designations:
        st.error("Enter at least one company and one designation.")
    else:
        if any(source in {"ddgs", "duckduckgo_browser"} for source in sources) and "google_browser" not in sources:
            sources = ["google_browser", *sources]
        config = {
            "companies": companies,
            "designations": designations,
            "locations": locations,
            "target_count": int(target_count),
            "sources": sources or list(DEFAULT_SOURCE_NAMES),
            "browser_headless": cloud_runtime or not headful,
            "google_manual_challenge_seconds": 180 if headful and manual_google_recovery and not cloud_runtime else 0,
            "max_results_per_search": 25,
            "retry_attempts": int(retry_attempts),
            "include_terms": _lines(include_terms_text),
            "exclude_terms": _lines(exclude_terms_text),
            "existing_files": [],
        }
        job_dir = create_job("company_pocs", config).resolve()
        config["existing_files"] = _save_uploads(existing_files, job_dir) + download_gsheet(gsheet_url, job_dir)
        write_json(job_dir / "config.json", config)
        launch_job(job_dir, "speedy_scraper.company_pocs")
        st.session_state.company_poc_job_dir = str(job_dir)
        st.rerun()

previous_jobs = list_jobs("company_pocs")
if previous_jobs:
    with st.expander("Open a previous or paused company POC job"):
        selected = st.selectbox(
            "Saved job",
            previous_jobs,
            format_func=lambda value: f"{value.name} · {read_status(value).get('state', 'unknown')}",
        )
        if st.button("Open selected company job"):
            st.session_state.company_poc_job_dir = str(selected.resolve())
            st.rerun()


@st.fragment(run_every="2s")
def job_monitor() -> None:
    raw_path = st.session_state.get("company_poc_job_dir", "")
    if not raw_path:
        st.info("Enter company names and designations, then start the background job.")
        return
    job_dir = Path(raw_path)
    status = read_status(job_dir)
    state = str(status.get("state") or "unknown")
    processed = int(status.get("processed") or 0)
    total = int(status.get("total") or 0)
    searches_completed = int(status.get("searches_completed") or 0)
    searches_total = int(status.get("searches_total") or 0)
    heartbeat_age = heartbeat_age_seconds(status)
    stale = job_is_stale(status)
    st.markdown(f"<div class='job-note'>JOB {job_dir.name}</div>", unsafe_allow_html=True)
    if searches_total:
        st.progress(
            min(searches_completed / searches_total, 1.0),
            text=f"{searches_completed}/{searches_total} individual searches completed",
        )
    elif total:
        st.progress(min(processed / total, 1.0), text=f"{processed}/{total} query groups")
    st.info(f"{state.upper()} · {status.get('message', '')}")
    captcha_recovery_panel(
        status,
        job_dir=job_dir,
        module="speedy_scraper.company_pocs",
        button_key="company_poc_captcha_recovery",
        launch_job=launch_job,
        request_stop=request_stop,
        read_status=read_status,
    )
    if stale:
        st.warning("The saved worker is no longer running. Relaunch it to continue from its checkpoint.")

    is_active = state in {"starting", "running", "stopping"}
    is_live = is_active and not stale and heartbeat_age is not None and heartbeat_age <= 8
    dot_class = "" if is_live else ("stale" if is_active else "done")
    tracker_title = "WORKER LIVE" if is_live else ("WAITING FOR HEARTBEAT" if is_active else state.upper())
    current = " · ".join(
        value
        for value in (
            str(status.get("current_company") or ""),
            str(status.get("current_designation") or ""),
            str(status.get("current_source") or ""),
        )
        if value
    ) or "Preparing the next public search"
    age_text = f"heartbeat {heartbeat_age}s ago" if heartbeat_age is not None else "heartbeat starting"
    st.markdown(
        f"<div class='live-ribbon'><span class='live-dot {dot_class}'></span><div>"
        f"<div class='live-title'>{html.escape(tracker_title)}</div>"
        f"<div class='live-detail'>{html.escape(current)} · {html.escape(age_text)}</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Tracker auto-reloaded at {datetime.now().strftime('%H:%M:%S')} · refreshes every 2 seconds"
    )

    left, right = st.columns(2)
    if state in {"running", "stopping"} and not stale:
        with left.container(key="company-stop-action"):
            if st.button(
                "Stop after current search",
                disabled=state == "stopping",
                width="stretch",
            ):
                request_stop(job_dir)
                st.rerun(scope="fragment")
    elif state in {"paused", "failed"} or stale:
        label = "Relaunch from checkpoint" if stale else "Resume from checkpoint"
        with left.container(key="company-resume-action"):
            if st.button(label, type="primary", width="stretch"):
                launch_job(job_dir, "speedy_scraper.company_pocs")
                st.rerun(scope="fragment")
    with right.container(key="company-refresh-action"):
        st.button("Refresh now", width="stretch")

    pocs, rejections = load_company_poc_checkpoint(job_dir)
    checkpoint = read_json(job_dir / "checkpoint.json", default={})
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Matched POCs", len(pocs))
    c2.metric("Searches complete", searches_completed or processed)
    c3.metric("Searches remaining", max((searches_total or total) - (searches_completed or processed), 0))

    verified_tab, review_tab, health_tab = st.tabs(
        ["Verified POCs", "Review queue", "Provider health"]
    )
    with verified_tab:
        if pocs:
            st.dataframe(company_pocs_frame(pocs), width="stretch", hide_index=True)
        else:
            st.caption("No verified POCs have cleared the company and designation gates yet.")
    with review_tab:
        review_frame = company_poc_review_frame(rejections)
        if not review_frame.empty:
            st.caption("Near-matches are reviewable here and are not included in verified exports.")
            st.dataframe(review_frame, width="stretch", hide_index=True)
        else:
            st.caption("No reviewable near-matches were recorded.")
        non_reviewable = [
            item for item in rejections
            if not bool(item.get("Reviewable", str(item.get("Reason") or "") in {"company_mismatch", "designation_mismatch", "invalid_name"}))
        ]
        if non_reviewable:
            with st.expander(f"Other rejected candidates ({len(non_reviewable)})"):
                st.dataframe(non_reviewable, width="stretch", hide_index=True)
    with health_tab:
        outcomes = status.get("provider_outcomes") or checkpoint.get("provider_outcomes") or {}
        health_rows = [
            {"Provider": name, **dict(outcome)}
            for name, outcome in outcomes.items()
        ]
        if health_rows:
            st.dataframe(health_rows, width="stretch", hide_index=True)
        st.metric("Retry attempts", int(status.get("retry_count") or 0))
        st.metric("Failed searches remaining", int(status.get("failed_searches") or len(checkpoint.get("retry_queue") or [])))
        source_errors = status.get("source_errors") or checkpoint.get("source_errors") or checkpoint.get("errors") or []
        if source_errors:
            with st.expander(f"Source errors ({len(source_errors)})"):
                for error in source_errors:
                    st.error(str(error))

    csv_path = Path(str(status.get("csv_path") or ""))
    xlsx_path = Path(str(status.get("xlsx_path") or ""))
    if csv_path.is_file() and xlsx_path.is_file():
        download_left, download_right = st.columns(2)
        download_left.download_button(
            "Download POCs CSV", csv_path.read_bytes(), csv_path.name, "text/csv",
            width="stretch",
        )
        download_right.download_button(
            "Download POCs Excel",
            xlsx_path.read_bytes(),
            xlsx_path.name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


job_monitor()
