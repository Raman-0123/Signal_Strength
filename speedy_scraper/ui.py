from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

import streamlit as st


def action_button_css() -> str:
    """Shared semantic action colors for all worker controls."""
    return """
    <style>
    :root {
        --action-run:#087d62;
        --action-run-hover:#06634f;
        --action-stop:#c43d2b;
        --action-stop-hover:#a93123;
        --action-resume:#1769aa;
        --action-resume-hover:#125582;
        --action-refresh:#52646b;
        --action-refresh-hover:#3d4e54;
        --action-text:#ffffff;
        --action-focus:#f3b63f;
    }
    [data-testid="stButton"] button,
    [data-testid="stFormSubmitButton"] button,
    [data-testid="stDownloadButton"] button {
        border:1px solid transparent!important;
        border-radius:10px!important;
        font-weight:700!important;
        min-height:2.6rem!important;
        transition:background .16s ease, box-shadow .16s ease, transform .16s ease!important;
    }
    [data-testid="stButton"] button:hover,
    [data-testid="stFormSubmitButton"] button:hover,
    [data-testid="stDownloadButton"] button:hover {
        transform:translateY(-1px);
        box-shadow:0 6px 16px rgba(24,49,44,.16)!important;
    }
    [data-testid="stButton"] button:focus-visible,
    [data-testid="stFormSubmitButton"] button:focus-visible,
    [data-testid="stDownloadButton"] button:focus-visible {
        outline:3px solid var(--action-focus)!important;
        outline-offset:2px!important;
    }
    .st-key-lead-run-action button,
    .st-key-url-run-action button,
    .st-key-company-run-action button {
        background:var(--action-run)!important;
        color:var(--action-text)!important;
    }
    .st-key-lead-run-action button:hover,
    .st-key-url-run-action button:hover,
    .st-key-company-run-action button:hover {
        background:var(--action-run-hover)!important;
    }
    .st-key-lead-stop-action button,
    .st-key-url-stop-action button,
    .st-key-company-stop-action button {
        background:var(--action-stop)!important;
        color:var(--action-text)!important;
    }
    .st-key-lead-stop-action button:hover,
    .st-key-url-stop-action button:hover,
    .st-key-company-stop-action button:hover {
        background:var(--action-stop-hover)!important;
    }
    .st-key-lead-resume-action button,
    .st-key-lead-network-retry-action button,
    .st-key-lead-challenge-retry-action button,
    .st-key-url-resume-action button,
    .st-key-company-resume-action button,
    [class*="st-key-"][class*="captcha_recovery"] button {
        background:var(--action-resume)!important;
        color:var(--action-text)!important;
    }
    .st-key-lead-resume-action button:hover,
    .st-key-lead-network-retry-action button:hover,
    .st-key-lead-challenge-retry-action button:hover,
    .st-key-url-resume-action button:hover,
    .st-key-company-resume-action button:hover,
    [class*="st-key-"][class*="captcha_recovery"] button:hover {
        background:var(--action-resume-hover)!important;
    }
    .st-key-lead-refresh-action button,
    .st-key-url-refresh-action button,
    .st-key-company-refresh-action button {
        background:var(--action-refresh)!important;
        color:var(--action-text)!important;
    }
    .st-key-lead-refresh-action button:hover,
    .st-key-url-refresh-action button:hover,
    .st-key-company-refresh-action button:hover {
        background:var(--action-refresh-hover)!important;
    }
    .st-key-company-delete-job-action button,
    .st-key-company-confirm-delete-action button,
    .st-key-lead-delete-job-action button,
    .st-key-lead-confirm-delete-action button {
        background:var(--action-stop)!important;
        color:var(--action-text)!important;
    }
    .st-key-company-delete-job-action button:hover,
    .st-key-company-confirm-delete-action button:hover,
    .st-key-lead-delete-job-action button:hover,
    .st-key-lead-confirm-delete-action button:hover {
        background:var(--action-stop-hover)!important;
    }
    .st-key-company-clear-jobs-action button,
    .st-key-company-confirm-clear-action button,
    .st-key-lead-clear-jobs-action button,
    .st-key-lead-confirm-clear-action button {
        background:#b7791f!important;
        color:var(--action-text)!important;
    }
    .st-key-company-clear-jobs-action button:hover,
    .st-key-company-confirm-clear-action button:hover,
    .st-key-lead-clear-jobs-action button:hover,
    .st-key-lead-confirm-clear-action button:hover {
        background:#8f5d18!important;
    }
    button:disabled {
        opacity:.48!important;
        transform:none!important;
        box-shadow:none!important;
    }
    </style>
    """


def render_theme_toggle(key: str) -> bool:
    """Render the small, per-page theme switch used by all Streamlit surfaces."""
    return bool(st.sidebar.toggle("Light mode", value=False, key=key))


def light_mode_css(enabled: bool) -> str:
    if not enabled:
        return ""
    return """
    <style>
    :root { --night:#f4f8f6; --night-2:#e9f2ef; --panel:#ffffff; --panel-2:#e3f0eb;
        --line:#bfd4cc; --text:#18312c; --muted:#55706a; --mint:#087d62;
        --sun:#9a6500; --coral:#c43d2b; --blue:#1769aa; --violet:#7540a6; }
    .stApp { color:var(--text)!important; background:
        radial-gradient(circle at 80% 2%, rgba(8,125,98,.10), transparent 28rem),
        linear-gradient(135deg,#f8fbfa,#edf6f2 58%,#f4f8f6)!important; }
    .stApp:before { opacity:.10!important; }
    [data-testid="stHeader"] { background:rgba(248,251,250,.90)!important; }
    [data-testid="stSidebar"] { background:#e7f1ed!important; }
    [data-testid="stSidebar"] * { color:#18312c!important; }
    h1,h2,h3,.hero h1,.url-hero-title { color:var(--text)!important; }
    p,.stCaption { color:var(--muted)!important; }
    .hero,.url-hero,.builder-panel,[data-testid="stForm"] { background:rgba(255,255,255,.84)!important; }
    .manifest-row,.contract,.phase,.live-strip,.live-ribbon,[data-testid="stMetric"] { background:rgba(255,255,255,.78)!important; }
    [data-baseweb="input"],[data-baseweb="select"],textarea { background:#fff!important; color:var(--text)!important; }
    .stTabs [aria-selected="true"] { background:var(--panel-2)!important; color:var(--mint)!important; }
    .stButton>button,.stDownloadButton>button,.stFormSubmitButton>button { background:#dcefe8!important; color:#123a30!important; }
    .stButton>button:hover,.stFormSubmitButton>button:hover,.stDownloadButton>button:hover { background:#c8e8dd!important; }
    </style>
    """


def enable_manual_recovery(job_dir: Path, *, extra_sources: list[str] | None = None) -> dict[str, Any]:
    """Persist a visible-browser Google recovery configuration for the next resume."""
    config_path = job_dir / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        config = {}
    if config.get("ui_version") not in {"google_only_v1", "multi_source_v2"}:
        # Migrate old lead jobs away from plans such as 80 queries x 5 pages.
        # New Google-only jobs keep the explicit 1-3 page choice from the form.
        config.update(
            {
                "max_queries": min(24, max(1, int(config.get("max_queries") or 20))),
                "max_results_per_query": 10,
                "max_pages_per_query": 1,
                "candidate_pool_multiplier": 2,
                "source_failure_limit": 1,
            }
        )
    config.update(
        {
            "sources": ["google_browser"],
            "browser_headless": False,
            "google_manual_challenge_seconds": 60,
        }
    )
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def is_streamlit_cloud() -> bool:
    """Detect the hosted Streamlit runtime without making local recovery unavailable."""
    if any(key in os.environ for key in ("STREAMLIT_SHARING_MODE", "STREAMLIT_CLOUD")):
        return True
    try:
        host = str(st.context.headers.get("host") or "").lower()
    except Exception:
        host = ""
    return host.endswith("streamlit.app") or host.endswith("streamlit.io")


def prepare_failed_search_retry(job_dir: Path, *, local_manual: bool) -> dict[str, Any]:
    """Mark a warning job for a targeted retry without resetting its checkpoint."""
    config = enable_manual_recovery(job_dir) if local_manual else _read_job_config(job_dir)
    if not local_manual:
        config.update(
            {
                "sources": ["google_browser"],
                "browser_headless": True,
                "google_manual_challenge_seconds": 0,
            }
        )
    config["retry_failed_searches"] = True
    (job_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def _read_job_config(job_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _open_manual_google_dialog(
    *,
    job_dir: Path,
    module: str,
    button_key: str,
    launch_job,
    request_stop,
) -> None:
    """Open a local-only modal explaining how the visible CAPTCHA recovery works."""

    @st.dialog("Manual Google CAPTCHA recovery")
    def recovery_dialog() -> None:
        st.warning(
            "Google challenged this search session. The CAPTCHA cannot be embedded in Streamlit, "
            "so the worker will open a visible local Chrome window for you."
        )
        st.markdown(
            "1. Click **Start visible Google recovery** below.\n"
            "2. In the Chrome window that opens, complete Google’s CAPTCHA.\n"
            "3. Leave Chrome open; the worker continues from the saved checkpoint."
        )
        st.caption("This option is available only when Streamlit is running on your computer.")
        if st.button(
            "Start visible Google recovery",
            key=f"{button_key}_start",
            type="primary",
            width="stretch",
        ):
            st.session_state.pop("_manual_google_recovery_dialog", None)
            prepare_failed_search_retry(job_dir, local_manual=True)
            request_stop(job_dir)
            launch_job(job_dir, module)
            st.rerun()

    recovery_dialog()


def render_pending_manual_google_dialog(*, launch_job, request_stop) -> None:
    """Render a queued recovery modal outside the auto-refresh job fragment."""
    pending = st.session_state.get("_manual_google_recovery_dialog")
    if not isinstance(pending, dict):
        return
    _open_manual_google_dialog(
        job_dir=Path(str(pending.get("job_dir") or "")),
        module=str(pending.get("module") or ""),
        button_key=str(pending.get("button_key") or "manual_google_recovery"),
        launch_job=launch_job,
        request_stop=request_stop,
    )


def captcha_recovery_panel(
    status: dict[str, Any],
    *,
    job_dir: Path,
    module: str,
    button_key: str,
    launch_job,
    request_stop,
    read_status,
) -> None:
    """Show an actionable recovery card when a public search source is challenged."""
    message = str(status.get("message") or "")
    errors = " ".join(str(item) for item in status.get("source_errors") or [])
    source = str(status.get("captcha_source") or "")
    warning_state = str(status.get("state") or "") == "completed_with_warnings"
    flagged = bool(status.get("captcha_required")) or "challenge" in f"{message} {errors}".lower()
    provider_failed = bool(status.get("fallback_recommended")) or bool(errors)
    if not flagged and not provider_failed and not warning_state:
        return
    cloud = is_streamlit_cloud()
    headline = (
        "Provider recovery required"
        if warning_state
        else "Manual Google recovery required"
        if source == "google_browser" and not cloud
        else "Provider fallback active"
    )
    if cloud:
        detail = (
            f"{source or 'A public search source'} failed or was challenged. The hosted worker cannot expose "
            "its Chrome window to your computer. Run this job locally to complete Google's verification."
        )
    else:
        if source == "google_browser":
            detail = (
                "Google was challenged. The visible local Google retry below reopens the saved searches "
                "and waits for you to solve the CAPTCHA."
            )
        else:
            detail = (
                f"{source or 'A public search source'} failed or was challenged. "
                "A visible local Google retry is available below."
            )
    st.warning(f"**{headline}**  \n{detail}")
    left, right = st.columns([1.25, 1])
    state = str(status.get("state") or "")
    if state in {"running", "starting", "stopping"}:
        left.info("Google is finishing the current search unit.")
    elif state in {"paused", "failed", "captcha_required", "completed", "completed_with_warnings"} or not status.get("pid"):
        failed_searches = int(status.get("failed_searches") or 0)
        if not failed_searches and state == "completed" and (provider_failed or flagged):
            # Old checkpoints only exposed source_errors; make them recoverable
            # after the new retry queue is deployed.
            failed_searches = 1
        if failed_searches:
            with left.container(key=f"{button_key}_action"):
                if cloud:
                    if st.button(
                        "Retry failed Google searches",
                        key=button_key,
                        type="primary",
                        width="stretch",
                    ):
                        prepare_failed_search_retry(job_dir, local_manual=False)
                        request_stop(job_dir)
                        launch_job(job_dir, module)
                        st.rerun(scope="fragment")
                elif st.button(
                    "Open manual Google CAPTCHA recovery",
                    key=button_key,
                    type="primary",
                    width="stretch",
                ):
                    st.session_state["_manual_google_recovery_dialog"] = {
                        "job_dir": str(job_dir),
                        "module": module,
                        "button_key": button_key,
                    }
                    st.rerun(scope="app")
        else:
            left.success("Fallback succeeded; no failed searches remain.")
    outcomes = status.get("provider_outcomes") or {}
    right.markdown("**Provider health**")
    if outcomes:
        for name, outcome in outcomes.items():
            right.caption(
                f"{name}: {outcome.get('results', 0)} results · "
                f"{outcome.get('errors', 0)} errors · {outcome.get('challenges', 0)} challenges"
            )
    elif errors:
        right.caption("Recorded provider failure:")
        right.caption(errors)
    else:
        right.caption("No provider telemetry recorded yet.")

def download_gsheet(url: str, job_dir: Path) -> list[str]:
    if not url or not url.strip():
        return []
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        st.error("Invalid Google Sheet URL format. Make sure it contains /spreadsheets/d/...")
        return []
    sheet_id = match.group(1)
    
    gid_match = re.search(r"[#?&]gid=([0-9]+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    upload_dir = job_dir / "dedupe_inputs"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"gsheet_{sheet_id}_{gid}.csv"
    
    try:
        urllib.request.urlretrieve(export_url, target)
        return [str(target.resolve())]
    except Exception as exc:
        st.error(f"Could not download Google Sheet: {exc}")
        return []
