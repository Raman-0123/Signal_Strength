from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

import streamlit as st


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
    sources = [str(item) for item in config.get("sources") or []]
    for source in [*(extra_sources or []), "google_browser"]:
        if source not in sources:
            sources.append(source)
    config.update(
        {
            "sources": sources,
            "browser_headless": False,
            "google_manual_challenge_seconds": 180,
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
        sources = [str(item) for item in config.get("sources") or []]
        if "google_browser" not in sources:
            sources.insert(0, "google_browser")
        config.update(
            {
                "sources": sources,
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
    headline = "Provider recovery required" if warning_state else "Provider fallback active"
    if cloud:
        detail = (
            f"{source or 'A public search source'} failed or was challenged. The hosted worker cannot expose "
            "its Chrome window to your computer, so use the automatic provider retry below."
        )
    else:
        detail = (
            f"{source or 'A public search source'} failed or was challenged. Google is available as a fallback; "
            "a visible local Chrome retry can be used when a CAPTCHA needs manual solving."
        )
    st.warning(f"**{headline}**  \n{detail}")
    left, right = st.columns([1.25, 1])
    state = str(status.get("state") or "")
    if state in {"running", "starting", "stopping"}:
        left.info("The worker is still trying fallback sources automatically.")
    elif state in {"paused", "failed", "captcha_required", "completed", "completed_with_warnings"} or not status.get("pid"):
        failed_searches = int(status.get("failed_searches") or 0)
        if not failed_searches and state == "completed" and (provider_failed or flagged):
            # Old checkpoints only exposed source_errors; make them recoverable
            # after the new retry queue is deployed.
            failed_searches = 1
        if failed_searches:
            label = "Retry failed searches automatically" if cloud else "Retry failed searches in visible Google"
            if left.button(label, key=button_key, type="primary", width="stretch"):
                prepare_failed_search_retry(job_dir, local_manual=not cloud)
                request_stop(job_dir)
                launch_job(job_dir, module)
                st.rerun(scope="fragment")
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
    
    gid_match = re.search(r"[#&]gid=([0-9]+)", url)
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
