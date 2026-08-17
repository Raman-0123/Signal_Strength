from __future__ import annotations

import hashlib
import html
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from speedy_scraper.background_jobs import (
    create_job,
    launch_job,
    list_jobs,
    read_json,
    read_status,
    update_status,
    write_json,
)
from speedy_scraper.outreach_intelligence import (
    CANONICAL_FIELDS,
    FIELD_LABELS,
    OutreachDecision,
    ReviewAction,
    SourceRole,
    SourceSpec,
    StatusCategory,
    build_outreach_frames,
    canonicalize_frame,
    detect_columns,
    fetch_public_google_sheet,
    load_review_decisions,
    read_source_bytes,
    save_review_decisions,
    suggest_status_category,
    validate_mapping,
    write_outreach_exports,
)
from speedy_scraper.outreach_job import load_outreach_checkpoint
from speedy_scraper.ui import action_button_css

st.set_page_config(
    page_title="Outreach Intelligence · Signal",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(action_button_css(), unsafe_allow_html=True)
st.markdown(
    """
    <style>
    :root {
        --oi-ink:#18211f;
        --oi-muted:#65706d;
        --oi-paper:#f4f1e8;
        --oi-card:#fffdf7;
        --oi-line:#d8d3c6;
        --oi-green:#087d62;
        --oi-gold:#d9a740;
        --oi-red:#b64838;
    }
    [data-testid="stAppViewContainer"] {
        background:
            linear-gradient(rgba(24,33,31,.028) 1px, transparent 1px),
            linear-gradient(90deg, rgba(24,33,31,.028) 1px, transparent 1px),
            var(--oi-paper);
        background-size:32px 32px;
        color:var(--oi-ink);
    }
    [data-testid="stHeader"] { background:rgba(244,241,232,.92); }
    [data-testid="stMainBlockContainer"] { max-width:1240px; padding-top:2.2rem; }
    h1,h2,h3 { color:var(--oi-ink); font-family:"Avenir Next",Avenir,sans-serif; }
    p,label,[data-testid="stCaptionContainer"] { font-family:"Avenir Next",Avenir,sans-serif; }
    .oi-hero {
        position:relative;
        overflow:hidden;
        border:1px solid var(--oi-ink);
        border-radius:2px;
        background:var(--oi-card);
        padding:34px 38px 30px;
        margin-bottom:22px;
        box-shadow:7px 7px 0 var(--oi-ink);
    }
    .oi-hero:after {
        content:"≠";
        position:absolute;
        right:24px;
        top:-35px;
        color:rgba(8,125,98,.11);
        font:800 170px/1 Georgia,serif;
    }
    .oi-eyebrow,.oi-micro {
        color:var(--oi-green);
        font-size:11px;
        font-weight:800;
        letter-spacing:.14em;
        text-transform:uppercase;
    }
    .oi-hero h1 {
        max-width:800px;
        margin:8px 0 6px;
        font:700 clamp(35px,5vw,60px)/.98 Georgia,serif;
        letter-spacing:-.045em;
    }
    .oi-hero p { max-width:760px; color:var(--oi-muted); margin:12px 0 0; }
    .oi-stages {
        display:grid;
        grid-template-columns:repeat(4,1fr);
        border:1px solid var(--oi-line);
        background:rgba(255,253,247,.72);
        margin:22px 0;
    }
    .oi-stage { padding:14px 16px; border-right:1px solid var(--oi-line); }
    .oi-stage:last-child { border-right:0; }
    .oi-stage b { display:block; font:700 17px Georgia,serif; }
    .oi-stage span { color:var(--oi-green); font-size:10px; letter-spacing:.12em; }
    .oi-section {
        display:flex;
        justify-content:space-between;
        align-items:end;
        border-bottom:1px solid var(--oi-ink);
        margin:30px 0 14px;
        padding-bottom:8px;
    }
    .oi-section h2 { margin:0; font:700 25px/1 Georgia,serif; }
    .oi-note {
        background:#dcebe4;
        border-left:4px solid var(--oi-green);
        padding:13px 15px;
        margin-bottom:16px;
        color:var(--oi-ink);
    }
    .oi-status {
        display:flex;
        justify-content:space-between;
        gap:22px;
        background:var(--oi-ink);
        color:#fff;
        padding:18px 22px;
        margin:12px 0 16px;
    }
    .oi-status small { color:#bdc8c4; }
    [data-testid="stMetric"] {
        background:rgba(255,253,247,.9);
        border-top:3px solid var(--oi-ink);
        padding:12px 14px;
    }
    [data-testid="stMetricValue"] { font-family:Georgia,serif; }
    [data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] {
        background:#fff!important;
        border-radius:2px!important;
    }
    .stAlert { border-radius:2px; }
    footer { visibility:hidden; }
    @media (max-width:760px) {
        .oi-hero { padding:25px 21px; box-shadow:4px 4px 0 var(--oi-ink); }
        .oi-stages { grid-template-columns:1fr 1fr; }
        .oi-stage:nth-child(2) { border-right:0; }
        .oi-stage:nth-child(-n+2) { border-bottom:1px solid var(--oi-line); }
        .oi-status { flex-direction:column; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def _read_cached(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    return read_source_bytes(data, filename)


def _source_id(name: str, data: bytes, role: SourceRole) -> str:
    digest = hashlib.sha256(data).hexdigest()[:12]
    return f"{role.value.casefold()}_{digest}_{Path(name).stem[:24]}"


def _uploaded_source(uploaded: Any, role: SourceRole) -> dict[str, Any]:
    data = bytes(uploaded.getvalue())
    return {
        "source_id": _source_id(str(uploaded.name), data, role),
        "source_name": str(uploaded.name),
        "data": data,
        "role": role,
        "origin": "upload",
    }


def _sheet_source(name: str, data: bytes, url: str) -> dict[str, Any]:
    return {
        "source_id": _source_id(name, data, SourceRole.PREVIOUS),
        "source_name": name,
        "data": data,
        "role": SourceRole.PREVIOUS,
        "origin": url,
    }


def _mapping_editor(source: dict[str, Any], default_region: str) -> dict[str, Any]:
    sheets = _read_cached(source["data"], source["source_name"])
    if not sheets:
        raise ValueError(f"{source['source_name']} has no readable sheets")
    sheet_names = list(sheets)
    sheet_key = f"oi_sheet_{source['source_id']}"
    selected_sheet = st.selectbox(
        "Sheet",
        sheet_names,
        key=sheet_key,
        help="Only the selected workbook sheet is included in this comparison.",
    )
    frame = sheets[selected_sheet]
    detection = detect_columns(frame.columns)
    st.caption(
        f"{len(frame):,} rows · {len(frame.columns)} columns · "
        f"{len(detection.mapping)} fields detected"
    )
    if detection.ambiguous:
        names = ", ".join(FIELD_LABELS[field] for field in detection.ambiguous)
        st.warning(f"Manual choice required for ambiguous fields: {names}")
    mapping: dict[str, str] = {}
    columns = [str(column) for column in frame.columns]
    groups = st.columns(3)
    for index, field_name in enumerate(CANONICAL_FIELDS):
        options = ["Not available", *columns]
        detected = detection.mapping.get(field_name, "Not available")
        widget_key = f"oi_map_{source['source_id']}_{selected_sheet}_{field_name}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = detected
        selected = groups[index % 3].selectbox(
            FIELD_LABELS[field_name],
            options,
            key=widget_key,
        )
        if selected != "Not available":
            mapping[field_name] = selected
    errors = validate_mapping(mapping, frame.columns)
    for message in errors:
        st.error(message)
    if not errors:
        preview_spec = SourceSpec(
            source_id=source["source_id"],
            source_name=source["source_name"],
            path="",
            sheet_name=selected_sheet,
            role=source["role"],
            mapping=mapping,
        )
        preview_records, preview_invalid = canonicalize_frame(
            frame,
            preview_spec,
            default_phone_region=default_region,
        )
        rejected_count = sum(row.disposition == "REJECTED" for row in preview_invalid)
        warning_count = sum(
            row.disposition == "PROCESSED_WITH_WARNING" for row in preview_invalid
        )
        st.caption(
            f"{len(preview_records):,} valid named rows · {rejected_count:,} rejected · "
            f"{warning_count:,} field warnings"
        )
    with st.expander("Preview mapped source", expanded=False):
        preview_columns = list(dict.fromkeys(mapping.values()))
        st.dataframe(
            frame[preview_columns].head(5) if preview_columns else frame.head(5),
            width="stretch",
            hide_index=True,
        )
    return {
        **source,
        "sheet_name": selected_sheet,
        "mapping": mapping,
        "frame": frame,
        "mapping_errors": errors,
    }


def _save_job_source(job_dir: Path, source: dict[str, Any], index: int) -> str:
    input_dir = job_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re_safe_filename(source["source_name"])
    target = input_dir / f"{index:03d}_{source['source_id']}_{safe_name}"
    target.write_bytes(source["data"])
    return str(target.resolve())


def re_safe_filename(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in ".-_" else "_" for character in value)
    return cleaned[:120] or "source.csv"


def _job_source_payload(source: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "source_id": source["source_id"],
        "source_name": source["source_name"],
        "path": path,
        "sheet_name": source["sheet_name"],
        "role": source["role"].value,
        "mapping": source["mapping"],
        "origin": source["origin"],
    }


def _filter_frame(frame: pd.DataFrame, search: str, decisions: list[str]) -> pd.DataFrame:
    filtered = frame
    if decisions and "Final Decision" in filtered.columns:
        filtered = filtered[filtered["Final Decision"].isin(decisions)]
    needle = search.strip().casefold()
    if needle and not filtered.empty:
        mask = filtered.fillna("").astype(str).apply(
            lambda column: column.str.casefold().str.contains(needle, regex=False)
        ).any(axis=1)
        filtered = filtered[mask]
    return filtered


def _download_csv(label: str, frame: pd.DataFrame, filename: str) -> None:
    st.download_button(
        label,
        frame.to_csv(index=False).encode("utf-8-sig"),
        filename,
        "text/csv",
        width="stretch",
    )


st.markdown(
    """
    <div class="oi-hero">
      <div class="oi-eyebrow">Signal / Outreach intelligence</div>
      <h1>Know who was contacted.<br>Protect the next outreach.</h1>
      <p>Compare a new prospect list with every colleague and event history. Exact evidence is matched automatically; uncertain identities stay visible for human review.</p>
    </div>
    <div class="oi-stages">
      <div class="oi-stage"><span>01 / INPUT</span><b>Sources</b></div>
      <div class="oi-stage"><span>02 / SCHEMA</span><b>Column mapping</b></div>
      <div class="oi-stage"><span>03 / POLICY</span><b>Status mapping</b></div>
      <div class="oi-stage"><span>04 / OUTPUT</span><b>Results</b></div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="oi-section"><h2>Sources</h2><span class="oi-micro">One primary · unlimited history</span></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="oi-note"><strong>Nothing is overwritten.</strong> Uploaded files and public Sheet tabs are copied into an auditable local run before matching begins.</div>',
    unsafe_allow_html=True,
)

source_left, source_right = st.columns(2, gap="large")
primary_upload = source_left.file_uploader(
    "Primary prospect list",
    type=["csv", "xlsx"],
    accept_multiple_files=False,
    help="The new people you may contact.",
)
previous_uploads = source_right.file_uploader(
    "Previous outreach files",
    type=["csv", "xlsx"],
    accept_multiple_files=True,
    help="Upload every colleague, event, and historical outreach export you want checked.",
)

sheet_left, sheet_right = st.columns([2, 1])
sheet_urls = sheet_left.text_area(
    "Public Google Sheet links — one per line",
    placeholder=(
        "https://docs.google.com/spreadsheets/d/.../edit#gid=0\n"
        "https://docs.google.com/spreadsheets/d/.../edit#gid=123"
    ),
    height=105,
)
if "oi_google_sources" not in st.session_state:
    st.session_state.oi_google_sources = []
with sheet_right:
    st.write("")
    st.write("")
    if st.button("Load public Sheets", width="stretch"):
        loaded: list[dict[str, Any]] = []
        for url in [line.strip() for line in sheet_urls.splitlines() if line.strip()]:
            try:
                name, data = fetch_public_google_sheet(url)
                loaded.append(_sheet_source(name, data, url))
            except ValueError as exc:
                st.error(f"{url}: {exc}")
        st.session_state.oi_google_sources = loaded
        if loaded:
            st.success(f"Loaded {len(loaded)} public Sheet tab(s).")
    if st.session_state.oi_google_sources and st.button(
        "Clear loaded Sheets", width="stretch"
    ):
        st.session_state.oi_google_sources = []
        st.rerun()

primary_source = _uploaded_source(primary_upload, SourceRole.PRIMARY) if primary_upload else None
previous_sources = [
    _uploaded_source(uploaded, SourceRole.PREVIOUS) for uploaded in previous_uploads or []
]
previous_sources.extend(st.session_state.oi_google_sources)

default_region = st.text_input(
    "Default phone country",
    value="IN",
    max_chars=2,
    help="Two-letter country code used only for phone numbers without an international prefix.",
).strip().upper()

mapped_primary: dict[str, Any] | None = None
mapped_previous: list[dict[str, Any]] = []
if primary_source or previous_sources:
    st.markdown(
        '<div class="oi-section"><h2>Column mapping</h2><span class="oi-micro">Name and POC resolve to one identity</span></div>',
        unsafe_allow_html=True,
    )
if primary_source:
    with st.expander(f"Primary · {primary_source['source_name']}", expanded=True):
        try:
            mapped_primary = _mapping_editor(primary_source, default_region)
        except Exception as exc:
            st.error(f"Could not read primary source: {exc}")
for source in previous_sources:
    origin = "Google Sheet" if source["origin"] != "upload" else "Previous file"
    with st.expander(f"{origin} · {source['source_name']}", expanded=False):
        try:
            mapped_previous.append(_mapping_editor(source, default_region))
        except Exception as exc:
            st.error(f"Could not read {source['source_name']}: {exc}")

status_map: dict[str, str] = {}
if mapped_previous:
    st.markdown(
        '<div class="oi-section"><h2>Status mapping</h2><span class="oi-micro">You control the outreach policy</span></div>',
        unsafe_allow_html=True,
    )
    discovered_statuses: list[str] = []
    for source in mapped_previous:
        status_column = source["mapping"].get("status")
        if status_column:
            discovered_statuses.extend(
                str(value).strip()
                for value in source["frame"][status_column].dropna().tolist()
                if str(value).strip()
            )
    discovered_statuses = sorted(set(discovered_statuses), key=str.casefold)
    if discovered_statuses:
        status_frame = pd.DataFrame(
            {
                "Raw status": discovered_statuses,
                "Canonical category": [
                    suggest_status_category(value).value for value in discovered_statuses
                ],
            }
        )
        status_digest = hashlib.sha256(
            "\n".join(discovered_statuses).encode("utf-8")
        ).hexdigest()[:10]
        edited_statuses = st.data_editor(
            status_frame,
            hide_index=True,
            width="stretch",
            disabled=["Raw status"],
            column_config={
                "Canonical category": st.column_config.SelectboxColumn(
                    "Canonical category",
                    options=[category.value for category in StatusCategory],
                    required=True,
                )
            },
            key=f"oi_status_mapping_editor_{status_digest}",
        )
        status_map = {
            str(row["Raw status"]): str(row["Canonical category"])
            for _, row in edited_statuses.iterrows()
        }
    else:
        st.info(
            "No previous status column is mapped. Confirmed historical matches will default "
            "to REVIEW REQUIRED until you decide otherwise."
        )

ready = bool(
    mapped_primary
    and mapped_previous
    and not mapped_primary["mapping_errors"]
    and all(not source["mapping_errors"] for source in mapped_previous)
    and len(default_region) == 2
)
if st.button(
    "Compare outreach history",
    type="primary",
    width="stretch",
    disabled=not ready,
):
    initial_config: dict[str, Any] = {
        "default_phone_region": default_region,
        "status_map": status_map,
        "primary": {},
        "previous": [],
    }
    job_dir = create_job("outreach_intelligence", initial_config).resolve()
    primary_path = _save_job_source(job_dir, mapped_primary, 0)
    previous_payloads = []
    for index, source in enumerate(mapped_previous, start=1):
        saved_path = _save_job_source(job_dir, source, index)
        previous_payloads.append(_job_source_payload(source, saved_path))
    initial_config["primary"] = _job_source_payload(mapped_primary, primary_path)
    initial_config["previous"] = previous_payloads
    write_json(job_dir / "config.json", initial_config)
    launch_job(job_dir, "speedy_scraper.outreach_job")
    st.session_state.oi_job_dir = str(job_dir)
    st.rerun()

previous_jobs = list_jobs("outreach_intelligence")
if "oi_job_dir" not in st.session_state:
    st.session_state.oi_job_dir = str(previous_jobs[0].resolve()) if previous_jobs else ""
if previous_jobs:
    with st.expander("Open an earlier comparison", expanded=False):
        selected_job = st.selectbox(
            "Saved comparison",
            previous_jobs,
            format_func=lambda value: (
                f"{value.name} · {read_status(value).get('state', 'unknown')}"
            ),
            label_visibility="collapsed",
        )
        if st.button("Open comparison", width="stretch"):
            st.session_state.oi_job_dir = str(selected_job.resolve())
            st.rerun()

st.markdown(
    '<div class="oi-section"><h2>Results</h2><span class="oi-micro">Every decision stays explainable</span></div>',
    unsafe_allow_html=True,
)


@st.fragment(run_every="2s")
def _result_monitor() -> None:
    raw_path = str(st.session_state.get("oi_job_dir") or "")
    if not raw_path:
        st.info("Upload a primary list and at least one previous-outreach source to begin.")
        return
    job_dir = Path(raw_path)
    status = read_status(job_dir)
    state = str(status.get("state") or "unknown")
    message = str(status.get("message") or "Preparing comparison")
    st.markdown(
        f"""
        <div class="oi-status">
          <strong>{html.escape(state.replace('_', ' ').upper())}</strong>
          <small>{html.escape(message)}</small>
        </div>
        """,
        unsafe_allow_html=True,
    )
    total = int(status.get("total") or 0)
    processed = int(status.get("processed") or 0)
    if state in {"queued", "starting", "running"}:
        if total:
            st.progress(min(processed / total, 1.0), text=f"{processed} of {total} units")
        else:
            st.info("The background worker is reading the selected sources.")
        return
    if state == "failed":
        st.error(message)
        log_path = job_dir / "worker.log"
        if log_path.exists():
            with st.expander("Technical details"):
                st.code(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
        return

    run, checkpoint = load_outreach_checkpoint(job_dir)
    if run is None:
        st.warning("The comparison has no readable checkpoint yet.")
        return
    config = read_json(job_dir / "config.json", default={})
    config = config if isinstance(config, dict) else {}
    persisted_overrides = load_review_decisions(job_dir)
    session_key = f"oi_review_{job_dir.name}"
    if session_key not in st.session_state:
        st.session_state[session_key] = persisted_overrides
    overrides = dict(st.session_state[session_key])
    frames = build_outreach_frames(
        run,
        overrides=overrides,
        status_map=dict(config.get("status_map") or {}),
    )

    metric_names = [
        ("Primary prospects", "primary_prospects"),
        ("Previous records", "previous_records"),
        ("Unique people", "unique_people"),
        ("Common people", "common_people"),
        ("Safe to contact", "safe_to_contact"),
        ("Already contacted", "already_contacted"),
        ("Do not contact", "do_not_contact"),
        ("Possible matches", "possible_matches"),
        ("Review required", "review_required"),
        ("Duplicates", "duplicates"),
    ]
    combined_decisions = (
        frames["combined"]["Final Decision"].fillna("").astype(str).value_counts()
        if "Final Decision" in frames["combined"].columns
        else pd.Series(dtype=int)
    )
    effective_metrics = {
        "primary_prospects": len(run.primary_records),
        "previous_records": len(run.previous_records),
        "unique_people": len(frames["combined"]),
        "common_people": frames["common"]["Primary Record ID"].nunique()
        if "Primary Record ID" in frames["common"].columns
        else 0,
        "safe_to_contact": len(frames["fresh"]),
        "already_contacted": int(
            combined_decisions.get(OutreachDecision.ALREADY_CONTACTED.value, 0)
        ),
        "do_not_contact": int(
            combined_decisions.get(OutreachDecision.DO_NOT_CONTACT.value, 0)
        ),
        "possible_matches": int(
            combined_decisions.get(OutreachDecision.POSSIBLE_MATCH.value, 0)
        ),
        "review_required": len(frames["review"]),
        "duplicates": int(run.metrics.get("duplicates", 0)),
    }
    for start in (0, 5):
        columns = st.columns(5)
        for column, (label, key) in zip(columns, metric_names[start : start + 5]):
            column.metric(label, int(effective_metrics.get(key, 0)))

    filter_left, filter_right = st.columns([2, 1])
    search = filter_left.text_input(
        "Filter result rows",
        placeholder="Search name, company, status, source, or reason",
        key=f"oi_search_{job_dir.name}",
    )
    decisions = filter_right.multiselect(
        "Final decisions",
        [decision.value for decision in OutreachDecision],
        key=f"oi_decisions_{job_dir.name}",
    )

    fresh_tab, common_tab, review_tab, combined_tab, invalid_tab = st.tabs(
        ["Fresh outreach", "Common people", "Review queue", "Combined master", "Rejected"]
    )
    with fresh_tab:
        fresh = _filter_frame(frames["fresh"], search, decisions)
        st.dataframe(fresh, width="stretch", hide_index=True)
        _download_csv("Download Fresh Outreach CSV", fresh, "Fresh_Outreach.csv")
    with common_tab:
        common = _filter_frame(frames["common"], search, decisions)
        st.dataframe(common, width="stretch", hide_index=True)
        _download_csv("Download Common People CSV", common, "Common_People.csv")
    with review_tab:
        review = _filter_frame(frames["review"], search, decisions)
        if review.empty:
            st.success("No uncertain identities require review.")
        else:
            editable_columns = [
                "Primary Record ID",
                "Name",
                "Company",
                "Previous Candidate IDs",
                "Previous Name",
                "Previous Company",
                "Previous Status",
                "Match Type",
                "Confidence",
                "Match Explanation",
                "Review Action",
            ]
            editable = review[[column for column in editable_columns if column in review.columns]]
            reviewed = st.data_editor(
                editable,
                hide_index=True,
                width="stretch",
                disabled=[column for column in editable.columns if column != "Review Action"],
                column_config={
                    "Review Action": st.column_config.SelectboxColumn(
                        "Review Action",
                        options=[action.value for action in ReviewAction],
                        required=True,
                    )
                },
                key=f"oi_review_editor_{job_dir.name}",
            )
            if st.button(
                "Save review decisions & rebuild exports",
                type="primary",
                width="stretch",
                key=f"oi_save_review_{job_dir.name}",
            ):
                updated = dict(overrides)
                for _, row in reviewed.iterrows():
                    updated[str(row["Primary Record ID"])] = str(row["Review Action"])
                save_review_decisions(job_dir, updated)
                st.session_state[session_key] = updated
                outputs = write_outreach_exports(
                    run,
                    job_dir,
                    overrides=updated,
                    status_map=dict(config.get("status_map") or {}),
                )
                checkpoint["outputs"] = outputs
                write_json(job_dir / "checkpoint.json", checkpoint)
                update_status(job_dir, **{**status, "outputs": outputs})
                st.success("Review decisions saved and every export was rebuilt.")
                st.rerun(scope="fragment")
        _download_csv("Download Review Queue CSV", review, "Review_Queue.csv")
    with combined_tab:
        combined = _filter_frame(frames["combined"], search, decisions)
        st.dataframe(combined, width="stretch", hide_index=True)
        _download_csv("Download Combined Master CSV", combined, "Combined_Master.csv")
    with invalid_tab:
        rejected = _filter_frame(frames["invalid"], search, decisions)
        if rejected.empty:
            st.success("No invalid or duplicate rows were recorded.")
        else:
            st.dataframe(rejected, width="stretch", hide_index=True)
        _download_csv(
            "Download Invalid / Rejected CSV", rejected, "Invalid_Rejected_Rows.csv"
        )

    outputs = checkpoint.get("outputs") if isinstance(checkpoint.get("outputs"), dict) else {}
    workbook = Path(str(outputs.get("xlsx") or ""))
    if workbook.is_file():
        st.download_button(
            "Download complete Outreach Intelligence workbook",
            workbook.read_bytes(),
            workbook.name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )


_result_monitor()
