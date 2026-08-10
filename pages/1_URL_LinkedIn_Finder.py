from __future__ import annotations

import html as _html
from datetime import datetime
from pathlib import Path

import streamlit as st

from speedy_scraper.background_jobs import (
    create_job,
    heartbeat_age_seconds,
    job_is_stale,
    launch_job,
    list_jobs,
    read_status,
    request_stop,
    write_json,
)
from speedy_scraper.event_speakers import speakers_frame
from speedy_scraper.models import DEFAULT_SOURCE_NAMES
from speedy_scraper.url_people_job import load_checkpoint_speakers
from speedy_scraper.ui import (
    captcha_recovery_panel,
    download_gsheet,
    enable_manual_recovery,
)

st.set_page_config(
    page_title="URL People LinkedIn Finder · Speedy Scraper",
    page_icon="◉",
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
# CSS removed to rely on Streamlit default light theme

# ─────────────────────────────────────────────────────────────────────────────
# Hero
# ─────────────────────────────────────────────────────────────────────────────
st.title("URL People LinkedIn Finder")
st.markdown(
    "Paste one or more public conference, speaker, or team pages (one URL per line). "
    "The worker extracts every person listed, then finds their LinkedIn profile "
    "through public search — no LinkedIn login, no API keys."
)

# ─────────────────────────────────────────────────────────────────────────────
# Form
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_URLS = ""

with st.form("url_people_form"):
    st.markdown('<div class="url-label">🌐 Conference / event page URLs — one per line</div>', unsafe_allow_html=True)
    source_urls_raw = st.text_area(
        "URLs",
        value=_DEFAULT_URLS,
        height=120,
        label_visibility="collapsed",
        placeholder="e.g. https://example.com/speakers\nhttps://another-event.com/speakers",
    )
    st.markdown(
        '<div class="url-hint">Each URL is fetched separately. People found across all pages are deduplicated by name + company before enrichment.</div>',
        unsafe_allow_html=True,
    )

    col_enrich, col_sources, col_head, col_recov = st.columns([1, 1.6, 0.8, 0.8])
    enrich_missing = col_enrich.checkbox("Search web for missing LinkedIn profiles", value=True)

    source_options = ["google_browser", "bing_browser", "duckduckgo_browser", "ddgs"]
    source_labels = {
        "google_browser": "Google browser",
        "bing_browser": "Bing browser",
        "duckduckgo_browser": "DuckDuckGo browser",
        "ddgs": "DDGS library",
    }
    sources = col_sources.multiselect(
        "Search sources",
        source_options,
        default=["google_browser"] + list(DEFAULT_SOURCE_NAMES),
        format_func=lambda v: source_labels[v],
        help="No LinkedIn login and no direct LinkedIn scraping.",
    )
    headful = col_head.checkbox("Show browser", value=False, help="Keep Chrome window visible.")
    manual_recovery = col_recov.checkbox(
        "Manual CAPTCHA recovery",
        value=False,
        disabled=not headful or "google_browser" not in sources,
        help="Wait up to 180s for manual CAPTCHA solving.",
    )
    include_terms_text = st.text_area(
        "Required search terms — one per line",
        placeholder="e.g. fintech\nleadership",
        height=80,
        help="Adds quoted context to the person lookup queries.",
    )
    exclude_terms_text = st.text_area(
        "Exclude search terms — one per line",
        placeholder="e.g. jobs\nrecruiter",
        height=80,
        help="Adds negative clauses to reduce irrelevant LinkedIn results.",
    )
    existing_files = st.file_uploader(
        "Prior POC/speaker exports to exclude",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help="Existing LinkedIn URLs and name/company identities are removed before enrichment.",
    )
    gsheet_url = st.text_input(
        "Or Google Sheet URL to exclude",
        placeholder="https://docs.google.com/spreadsheets/d/...",
        help="Paste a public Google Sheet URL to exclude existing POCs from the search.",
    )

    submitted = st.form_submit_button("⚡ Start URL People Job", type="primary", use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# Job launch
# ─────────────────────────────────────────────────────────────────────────────
if "url_people_job_dir" not in st.session_state:
    st.session_state.url_people_job_dir = ""


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

if submitted:
    urls = [u.strip() for u in source_urls_raw.splitlines() if u.strip()]
    if not urls:
        st.error("Enter at least one URL.")
    else:
        if any(source in {"ddgs", "duckduckgo_browser"} for source in sources) and "google_browser" not in sources:
            sources = ["google_browser", *sources]
        job_config = {
            "source_urls": urls,
            "source_url": urls[0],  # backward-compat
            "enrich_missing": enrich_missing,
            "sources": sources or list(DEFAULT_SOURCE_NAMES),
            "browser_headless": not headful,
            "google_manual_challenge_seconds": (180 if headful and manual_recovery else 0),
            "include_terms": _lines(include_terms_text),
            "exclude_terms": _lines(exclude_terms_text),
            "existing_files": [],
        }
        job_dir = create_job("url_people", job_config).resolve()
        job_config["existing_files"] = _save_uploads(existing_files, job_dir) + download_gsheet(gsheet_url, job_dir)
        write_json(job_dir / "config.json", job_config)
        launch_job(job_dir, "speedy_scraper.url_people_job")
        st.session_state.url_people_job_dir = str(job_dir)
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Job archive
# ─────────────────────────────────────────────────────────────────────────────
previous_jobs = list_jobs("url_people")
if previous_jobs:
    with st.expander("Open a previous or paused URL job", expanded=False):
        selected = st.selectbox(
            "Saved job",
            previous_jobs,
            format_func=lambda v: f"{v.name} · {read_status(v).get('state', 'unknown')}",
        )
        col1, col2, col3 = st.columns([1, 1, 1])
        if col1.button("Open selected job"):
            st.session_state.url_people_job_dir = str(selected.resolve())
            st.rerun()
        if col2.button("❌ Delete selected job"):
            import shutil
            shutil.rmtree(selected)
            if st.session_state.get("url_people_job_dir") == str(selected.resolve()):
                st.session_state.url_people_job_dir = ""
            st.rerun()
        if col3.button("🗑️ Clear all jobs"):
            import shutil
            for j in previous_jobs:
                shutil.rmtree(j)
            st.session_state.url_people_job_dir = ""
            st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Live job monitor
# ─────────────────────────────────────────────────────────────────────────────
@st.fragment(run_every="2s")
def job_monitor() -> None:
    raw_path = st.session_state.get("url_people_job_dir", "")
    if not raw_path:
        st.info("Start a job above — you can leave this page while it runs.")
        return

    job_dir = Path(raw_path)
    status = read_status(job_dir)
    state = str(status.get("state") or "unknown")
    processed = int(status.get("processed") or 0)
    total = int(status.get("total") or 0)
    heartbeat_age = heartbeat_age_seconds(status)
    stale = job_is_stale(status)

    st.markdown(f"<div class='job-note'>Job · {job_dir.name}</div>", unsafe_allow_html=True)

    if total:
        st.progress(min(processed / total, 1.0), text=f"{processed}/{total} people processed")

    is_active = state in {"starting", "running", "stopping"}
    is_live = is_active and not stale and heartbeat_age is not None and heartbeat_age <= 8
    dot_class = "" if is_live else ("stale" if is_active else "done")
    tracker_title = "WORKER LIVE" if is_live else ("WAITING FOR HEARTBEAT" if is_active else state.upper())
    current = " · ".join(
        v for v in (
            str(status.get("current_name") or ""),
            str(status.get("current_company") or ""),
            str(status.get("activity") or ""),
        ) if v
    ) or "Preparing the next person"
    age_text = f"heartbeat {heartbeat_age}s ago" if heartbeat_age is not None else "heartbeat starting"
    msg = str(status.get("message") or "")

    st.markdown(
        f"""<div class='live-ribbon'>
              <span class='live-dot {dot_class}'></span>
              <div>
                <div class='live-title'>{_html.escape(tracker_title)}</div>
                <div class='live-detail'>{_html.escape(current)} · {_html.escape(age_text)}</div>
                {f"<div class='live-detail' style='color:#94aaa8;margin-top:3px'>{_html.escape(msg)}</div>" if msg else ""}
              </div>
            </div>""",
        unsafe_allow_html=True,
    )
    if stale:
        st.warning("The saved worker is no longer running. Relaunch it to continue from its checkpoint.")

    captcha_recovery_panel(
        status,
        job_dir=job_dir,
        module="speedy_scraper.url_people_job",
        button_key="url_people_captcha_recovery",
        launch_job=launch_job,
        request_stop=request_stop,
        read_status=read_status,
    )

    st.caption(f"Auto-refreshes every 2 s · {datetime.now().strftime('%H:%M:%S')}")

    ctrl_l, ctrl_r, ctrl_meta = st.columns([1, 1, 2])
    if state in {"running", "stopping"} and not stale:
        if ctrl_l.button("Stop after current person", disabled=state == "stopping", width="stretch"):
            request_stop(job_dir)
            st.rerun(scope="fragment")
    elif state in {"paused", "failed"} or stale:
        label = "Relaunch from checkpoint" if stale else "Resume from checkpoint"
        if ctrl_l.button(label, type="primary", width="stretch"):
            launch_job(job_dir, "speedy_scraper.url_people_job")
            st.rerun(scope="fragment")
    ctrl_r.button("Refresh now", width="stretch")
    ctrl_meta.caption(f"updated {datetime.now().strftime('%H:%M:%S')}")

    # Metrics
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Extracted", total, help="Total people found across all URLs")
    m2.metric("Provided", int(status.get("provided") or 0), help="LinkedIn already on the page")
    m3.metric("Matched", int(status.get("matched") or 0), help="LinkedIn found via search")
    m4.metric("Ambiguous", int(status.get("ambiguous") or 0), help="Multiple candidates, low confidence")
    m5.metric("Remaining", max(total - processed, 0))

    # Results tabs
    speakers, checkpoint_index = load_checkpoint_speakers(job_dir)
    if speakers:
        results_tab, breakdown_tab = st.tabs(["People & LinkedIn URLs", "Status breakdown"])
        with results_tab:
            visible = speakers if state == "completed" else speakers[:checkpoint_index]
            st.dataframe(speakers_frame(visible), use_container_width=True, hide_index=True)
        with breakdown_tab:
            from speedy_scraper.event_speakers import count_status
            visible_all = speakers if state == "completed" else speakers[:checkpoint_index]
            breakdown = [
                {"Status": "provided (on page)", "Count": count_status(visible_all, "provided")},
                {"Status": "matched (via search)", "Count": count_status(visible_all, "matched")},
                {"Status": "ambiguous", "Count": count_status(visible_all, "ambiguous")},
                {"Status": "not_found", "Count": count_status(visible_all, "not_found")},
            ]
            st.dataframe(breakdown, use_container_width=True, hide_index=True)

    # Downloads
    csv_path = Path(str(status.get("csv_path") or ""))
    xlsx_path = Path(str(status.get("xlsx_path") or ""))
    if csv_path.is_file() and xlsx_path.is_file():
        dl_a, dl_b = st.columns(2)
        dl_a.download_button(
            "⬇ Download CSV",
            csv_path.read_bytes(),
            csv_path.name,
            "text/csv",
            width="stretch",
        )
        dl_b.download_button(
            "⬇ Download Excel workbook",
            xlsx_path.read_bytes(),
            xlsx_path.name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )


job_monitor()
