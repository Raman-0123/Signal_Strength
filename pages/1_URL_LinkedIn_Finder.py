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
    light_mode_css,
    render_theme_toggle,
)

st.set_page_config(
    page_title="URL People LinkedIn Finder · Speedy Scraper",
    page_icon="◉",
    layout="wide",
)
light_mode = render_theme_toggle("url_people_light_mode")

# ─────────────────────────────────────────────────────────────────────────────
# CSS — matches the dark glassmorphism theme of the main app
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
    :root {
        --night:#071316; --night-2:#0b1d21; --panel:#10282d; --panel-2:#15343a;
        --line:#28515a; --text:#eef7f4; --muted:#94aaa8; --mint:#72f2c3;
        --sun:#ffc857; --coral:#ff785a; --blue:#73b7ff; --violet:#c87cf9;
    }
    .stApp {color:var(--text); background:
        radial-gradient(circle at 20% 5%, rgba(114,242,195,.09), transparent 26rem),
        linear-gradient(135deg, var(--night), #08191d 58%, #071215);}
    .stApp:before {content:"";position:fixed;inset:0;pointer-events:none;opacity:.18;
        background-image:linear-gradient(rgba(115,183,255,.08) 1px,transparent 1px),
        linear-gradient(90deg,rgba(115,183,255,.08) 1px,transparent 1px);
        background-size:44px 44px;mask-image:linear-gradient(to bottom,black,transparent 70%);}
    [data-testid="stHeader"] {background:rgba(7,19,22,.84);border-bottom:1px solid var(--line);backdrop-filter:blur(18px);}
    [data-testid="stSidebar"] {background:#061013;border-right:1px solid var(--line);}
    [data-testid="stSidebar"] * {color:#d9e8e5;}
    .block-container {max-width:1320px;padding-top:2.2rem;padding-bottom:5rem;}
    h1,h2,h3 {font-family:'Avenir Next','Futura',sans-serif!important;color:var(--text)!important;letter-spacing:-.035em;}
    p,label,div,button,input,textarea,caption {font-family:'SFMono-Regular','Menlo',monospace;}
    p,.stCaption {color:var(--muted);}

    /* Page hero strip */
    .url-hero {border:1px solid var(--line);border-radius:20px;padding:28px 32px;margin-bottom:28px;
        background:linear-gradient(125deg,rgba(21,52,58,.9),rgba(8,24,28,.9));
        display:flex;gap:28px;align-items:flex-start;position:relative;overflow:hidden;}
    .url-hero:after {content:"";position:absolute;width:220px;height:220px;border-radius:50%;
        right:-70px;top:-110px;border:40px solid rgba(114,242,195,.1);}
    .url-hero-icon {font-size:38px;line-height:1;flex:none;}
    .url-hero-title {font:700 clamp(22px,3vw,34px)/1.1 'Avenir Next','Futura',sans-serif;
        color:var(--text);margin:0 0 8px;letter-spacing:-.03em;}
    .url-hero-title em {font-style:normal;color:var(--mint);}
    .url-hero-copy {font-size:12px;line-height:1.75;color:#8fb0ad;max-width:640px;}
    .kicker {font:600 10px/1.5 'SFMono-Regular','Menlo',monospace;letter-spacing:.17em;
        text-transform:uppercase;color:var(--mint);margin-bottom:8px;}

    /* Supported sites pill strip */
    .site-pill {display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:99px;
        font:500 10px 'SFMono-Regular','Menlo',monospace;letter-spacing:.05em;
        background:rgba(114,242,195,.08);color:var(--mint);border:1px solid rgba(114,242,195,.2);
        margin:0 4px 4px 0;}

    /* Form */
    [data-testid="stForm"] {border:1px solid var(--line);border-radius:18px;padding:20px 22px;
        background:rgba(11,29,33,.72);box-shadow:0 14px 45px rgba(0,0,0,.16);}
    [data-baseweb="input"],[data-baseweb="select"],textarea {
        border-radius:10px!important;background:#0b2024!important;}
    .stButton>button,.stDownloadButton>button,.stFormSubmitButton>button {
        border-radius:12px!important;border:1px solid #39756b!important;
        background:#173e39!important;color:#edfff9!important;
        text-transform:uppercase!important;letter-spacing:.07em!important;
        min-height:44px;transition:transform .18s ease,background .18s ease,box-shadow .18s ease;}
    .stButton>button:hover,.stFormSubmitButton>button:hover,.stDownloadButton>button:hover {
        background:#20594f!important;border-color:var(--mint)!important;
        transform:translateY(-1px);box-shadow:0 8px 22px rgba(0,0,0,.22);}

    /* Metrics */
    [data-testid="stMetric"] {min-height:100px;background:rgba(16,40,45,.72);
        border:1px solid var(--line);border-radius:13px;padding:14px;}
    [data-testid="stMetricLabel"] {text-transform:uppercase;letter-spacing:.08em;color:#8fa5a2;font-size:10px;}
    [data-testid="stMetricValue"] {font-family:'Avenir Next','Futura',sans-serif;color:var(--text);}
    [data-testid="stMetricDelta"] {font-size:11px;}

    /* Live ribbon */
    .live-ribbon {display:flex;gap:13px;align-items:flex-start;
        border:1px solid var(--line);background:rgba(11,29,33,.88);
        padding:14px 16px;margin:10px 0;border-radius:13px;}
    .live-dot {width:9px;height:9px;border-radius:50%;flex:none;margin-top:3px;
        background:var(--mint);box-shadow:0 0 0 0 rgba(114,242,195,.45);animation:pulse 1.7s infinite;}
    .live-dot.stale {background:var(--coral);animation:none;}
    .live-dot.done {background:#6c8582;animation:none;}
    .live-title {font:700 11px 'SFMono-Regular','Menlo',monospace;letter-spacing:.1em;
        text-transform:uppercase;color:var(--text);}
    .live-detail {color:#68675f;font-size:11px;margin-top:3px;}
    @keyframes pulse {70%{box-shadow:0 0 0 9px rgba(114,242,195,0);}100%{box-shadow:0 0 0 0 rgba(114,242,195,0);}}

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {gap:6px;border-bottom:1px solid var(--line);}
    .stTabs [data-baseweb="tab"] {border-radius:10px 10px 0 0;padding:10px 16px;
        text-transform:uppercase;font-size:10px;}
    .stTabs [aria-selected="true"] {background:var(--panel-2);color:var(--mint);}
    [data-testid="stDataFrame"] {border:1px solid var(--line);border-radius:12px;overflow:hidden;}

    /* URL input label */
    .url-label {font:600 10px 'SFMono-Regular','Menlo',monospace;letter-spacing:.15em;
        text-transform:uppercase;color:var(--blue);margin-bottom:4px;}
    .url-hint {font-size:10px;color:#5a7672;line-height:1.6;margin-top:4px;}

    /* Job note */
    .job-note {font:500 10px 'SFMono-Regular','Menlo',monospace;letter-spacing:.08em;
        color:#52605a;margin-bottom:6px;text-transform:uppercase;}

    @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(light_mode_css(light_mode), unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Hero
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="url-hero">
      <div class="url-hero-icon">🔍</div>
      <div>
        <div class="kicker">Conference & Event Intelligence</div>
        <div class="url-hero-title">URL People <em>LinkedIn Finder</em></div>
        <div class="url-hero-copy">
          Paste one or more public conference, speaker, or team pages (one URL per line).
          The worker extracts every person listed, then finds their LinkedIn profile
          through public search — no LinkedIn login, no API keys.
        </div>
        <div style="margin-top:14px;">
          <span class="site-pill">🎤 Speaker pages</span>
          <span class="site-pill">📋 Divi/Elementor events</span>
          <span class="site-pill">🔗 Pages with LinkedIn links</span>
          <span class="site-pill">📦 Next.js JSON events</span>
          <span class="site-pill">🗂 JSON-LD Person markup</span>
          <span class="site-pill">🃏 HTML card grids</span>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# Form
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_URLS = (
    "https://www.intriguesummit.com/madverse2026singapore\n"
    "https://conferences.marketing-interactive.com/digital-marketing-asia/"
)

with st.form("url_people_form"):
    st.markdown('<div class="url-label">🌐 Conference / event page URLs — one per line</div>', unsafe_allow_html=True)
    source_urls_raw = st.text_area(
        "URLs",
        value=_DEFAULT_URLS,
        height=120,
        label_visibility="collapsed",
        placeholder="https://example.com/speakers\nhttps://another-event.com/speakers",
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
        if st.button("Open selected job"):
            st.session_state.url_people_job_dir = str(selected.resolve())
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
