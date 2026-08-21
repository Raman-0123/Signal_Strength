from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import select

from speedy_scraper.campaign_db import session_scope
from speedy_scraper.campaign_models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    EmailAccount,
    ResponseClassification,
)
from speedy_scraper.email_campaigns import (
    CampaignSchedule,
    campaign_configuration_frame,
    campaign_history_frame,
    campaign_metrics,
    campaign_query,
    campaign_workbook,
    cancel_campaign,
    candidates_from_frame,
    classify_response,
    configure_campaign,
    create_campaign,
    events_frame,
    pause_campaign,
    recipient_context,
    recipients_frame,
    render_template,
    resume_campaign,
    schedule_campaign,
    validate_templates,
)
from speedy_scraper.email_sender import (
    GmailSender,
    build_email_message,
    build_gmail_authorization_url,
    rfc_message_id,
)
from speedy_scraper.outreach_intelligence import detect_columns, normalize_email, read_source_bytes
from speedy_scraper.ui import action_button_css

st.set_page_config(
    page_title="Email Campaigns · Signal",
    page_icon="✉",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown(action_button_css(), unsafe_allow_html=True)
st.markdown(
    """
    <style>
    :root{--ec-ink:#17211e;--ec-paper:#f4f1e8;--ec-card:#fffdf7;--ec-line:#d3cec0;
      --ec-green:#087d62;--ec-gold:#dda83b;--ec-red:#b64838;--ec-blue:#1769aa;--ec-muted:#69736f}
    [data-testid="stAppViewContainer"]{background:
      linear-gradient(rgba(23,33,30,.03) 1px,transparent 1px),
      linear-gradient(90deg,rgba(23,33,30,.03) 1px,transparent 1px),var(--ec-paper);
      background-size:32px 32px;color:var(--ec-ink)}
    [data-testid="stHeader"]{background:rgba(244,241,232,.94)}
    [data-testid="stMainBlockContainer"]{max-width:1280px;padding-top:2rem}
    h1,h2,h3{color:var(--ec-ink)}
    .ec-hero{position:relative;overflow:hidden;border:1px solid var(--ec-ink);background:var(--ec-card);
      padding:34px 38px 30px;margin-bottom:22px;box-shadow:7px 7px 0 var(--ec-ink)}
    .ec-hero:after{content:"↗";position:absolute;right:24px;top:-42px;color:rgba(8,125,98,.10);
      font:800 180px/1 Georgia,serif}
    .ec-kicker,.ec-micro{color:var(--ec-green);font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}
    .ec-hero h1{max-width:850px;margin:8px 0 6px;font:700 clamp(35px,5vw,60px)/.98 Georgia,serif;letter-spacing:-.045em}
    .ec-hero p{max-width:780px;color:var(--ec-muted);margin:13px 0 0}
    .ec-stages{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--ec-line);
      background:rgba(255,253,247,.76);margin:22px 0}
    .ec-stage{padding:14px 16px;border-right:1px solid var(--ec-line)}.ec-stage:last-child{border:0}
    .ec-stage b{display:block;font:700 17px Georgia,serif}.ec-stage span{color:var(--ec-green);font-size:10px;letter-spacing:.12em}
    .ec-band{display:flex;justify-content:space-between;gap:20px;align-items:end;border-bottom:1px solid var(--ec-ink);margin:30px 0 14px;padding-bottom:8px}
    .ec-band h2{margin:0;font:700 26px/1 Georgia,serif}
    .ec-note{border-left:4px solid var(--ec-green);background:#dcebe4;padding:13px 15px;margin:12px 0;color:var(--ec-ink)}
    .ec-warning{border-left-color:var(--ec-gold);background:#f3ead0}
    .ec-live{display:flex;justify-content:space-between;gap:20px;background:var(--ec-ink);color:white;padding:18px 22px;margin:12px 0 16px}
    .ec-live small{color:#bec8c4}.ec-live strong{letter-spacing:.06em}
    [data-testid="stMetric"]{background:rgba(255,253,247,.92);border-top:3px solid var(--ec-ink);padding:12px 14px}
    [data-testid="stMetricValue"]{font-family:Georgia,serif}
    [data-baseweb="input"],[data-baseweb="textarea"],[data-baseweb="select"]{background:#fff!important;border-radius:2px!important}
    [data-testid="stForm"]{background:rgba(255,253,247,.72);border:1px solid var(--ec-line);padding:18px}
    .stAlert{border-radius:2px}footer{visibility:hidden}
    @media(max-width:760px){.ec-hero{padding:25px 21px;box-shadow:4px 4px 0 var(--ec-ink)}
      .ec-stages{grid-template-columns:1fr 1fr}.ec-stage:nth-child(2){border-right:0}.ec-stage:nth-child(-n+2){border-bottom:1px solid var(--ec-line)}
      .ec-live,.ec-band{align-items:start;flex-direction:column}}
    </style>
    """,
    unsafe_allow_html=True,
)


def _require_access() -> None:
    expected = os.environ.get("ADMIN_PASSWORD_SHA256", "").strip().casefold()
    if not expected:
        if os.environ.get("RENDER"):
            st.error("ADMIN_PASSWORD_SHA256 must be configured before this cloud page can open.")
            st.stop()
        st.info("Local development mode · configure ADMIN_PASSWORD_SHA256 before cloud deployment.")
        return
    if st.session_state.get("email_admin_authenticated"):
        return
    st.markdown("### Private campaign workspace")
    password = st.text_input("Admin password", type="password")
    if st.button("Unlock campaign workspace", type="primary"):
        digest = hashlib.sha256(password.encode()).hexdigest()
        if secrets.compare_digest(digest, expected):
            st.session_state["email_admin_authenticated"] = True
            st.rerun()
        st.error("Password is incorrect.")
    st.stop()


@st.cache_data(show_spinner=False)
def _read_upload(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    return read_source_bytes(data, filename)


def _guess(columns: list[str], *names: str) -> str:
    normalized = {column.casefold().strip(): column for column in columns}
    for name in names:
        if name.casefold() in normalized:
            return normalized[name.casefold()]
    return ""


def _column_mapping(frame: pd.DataFrame, prefix: str) -> dict[str, str]:
    columns = [str(column) for column in frame.columns]
    detected = detect_columns(columns).mapping
    choices = ["", *columns]
    fields = [
        ("email", "Email", detected.get("email", "")),
        ("full_name", "Full name", detected.get("full_name", "")),
        ("first_name", "First name", detected.get("first_name", "")),
        ("last_name", "Last name", detected.get("last_name", "")),
        ("company", "Company", detected.get("company", "")),
        ("designation", "Designation", detected.get("designation", "")),
        ("linkedin_url", "LinkedIn URL", detected.get("linkedin_url", "")),
        ("decision", "Final decision", _guess(columns, "Final Decision", "Decision")),
        ("record_id", "Record ID", _guess(columns, "Primary Record ID", "Record ID")),
    ]
    mapping: dict[str, str] = {}
    grid = st.columns(3)
    for index, (field, label, suggested) in enumerate(fields):
        selected = grid[index % 3].selectbox(
            label,
            choices,
            index=choices.index(suggested) if suggested in choices else 0,
            key=f"{prefix}_{field}",
        )
        if selected:
            mapping[field] = selected
    return mapping


def _selected_campaign(session) -> Campaign | None:
    campaigns = list(session.scalars(campaign_query()).all())
    if not campaigns:
        return None
    preferred = str(st.session_state.get("email_campaign_id") or "")
    ids = [campaign.id for campaign in campaigns]
    selected = st.selectbox(
        "Open campaign",
        ids,
        index=ids.index(preferred) if preferred in ids else 0,
        format_func=lambda value: next(
            f"{item.name} · {item.status}" for item in campaigns if item.id == value
        ),
        key="email_campaign_selector",
    )
    st.session_state["email_campaign_id"] = selected
    return session.get(Campaign, selected)


def _create_from_upload() -> None:
    upload = st.file_uploader(
        "Recipient CSV or Excel",
        type=["csv", "xlsx"],
        help="Campaign size is unrestricted. Every row is validated and deduplicated by email.",
    )
    if upload is None:
        return
    sheets = _read_upload(bytes(upload.getvalue()), str(upload.name))
    selected_sheet = st.selectbox("Sheet", list(sheets), key="email_source_sheet")
    frame = sheets[selected_sheet]
    st.caption(f"{len(frame):,} rows · {len(frame.columns)} columns")
    mapping = _column_mapping(frame, "email_map")
    has_decision = bool(mapping.get("decision"))
    confirmed_safe = st.checkbox(
        "I confirm that every included row is safe and permitted for outreach",
        disabled=has_decision,
        help="When a Final Decision column is mapped, only SAFE TO CONTACT rows are accepted.",
    )
    name = st.text_input("Campaign name", value=f"Campaign · {datetime.now():%d %b %Y}")
    if st.button("Validate recipients & create draft", type="primary", width="stretch"):
        try:
            candidates, rejected = candidates_from_frame(
                frame, mapping, confirmed_safe=confirmed_safe
            )
            if not candidates:
                raise ValueError("No eligible recipient has a valid email address")
            with session_scope() as session:
                campaign, suppressed = create_campaign(
                    session,
                    name=name,
                    candidates=candidates,
                    source_reference=f"upload:{upload.name}:{selected_sheet}",
                )
                campaign_id = campaign.id
            st.session_state["email_campaign_id"] = campaign_id
            st.success(
                f"Draft created with {len(candidates) - len(suppressed):,} recipients. "
                f"{len(rejected) + len(suppressed):,} rows were rejected or suppressed."
            )
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _compose(session, campaign: Campaign) -> None:
    recipients = list(
        session.scalars(
            select(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign.id)
            .order_by(CampaignRecipient.created_at)
        ).all()
    )
    if not recipients:
        st.warning("This campaign has no eligible recipients.")
        return
    available = sorted(recipient_context(recipients[0]))
    st.caption("Available personalization: " + " · ".join(f"{{{{{key}}}}}" for key in available))
    editable = campaign.status == CampaignStatus.DRAFT.value
    with st.form(f"compose_{campaign.id}"):
        subject = st.text_input(
            "Subject",
            value=campaign.subject_template or "A focused invitation for {{first_name}}",
            disabled=not editable,
        )
        body = st.text_area(
            "HTML email body",
            value=campaign.html_template
            or (
                "<p>Hi {{first_name}},</p>\n"
                "<p>I’m reaching out because of your work as {{designation}} at {{company}}.</p>\n"
                "<p>I’d value the chance to share a focused invitation with you.</p>\n"
                "<p>Best,<br>Sender name</p>"
            ),
            height=240,
            disabled=not editable,
        )
        text_body = st.text_area(
            "Plain-text fallback",
            value=campaign.text_template
            or (
                "Hi {{first_name}},\n\nI’m reaching out because of your work as {{designation}} "
                "at {{company}}.\n\nI’d value the chance to share a focused invitation with you.\n\nBest,\nSender name"
            ),
            height=170,
            disabled=not editable,
        )
        left, right = st.columns(2)
        sender_name = left.text_input(
            "Sender name", value=campaign.sender_name, disabled=not editable
        )
        reply_to = right.text_input(
            "Reply-to email", value=campaign.reply_to, disabled=not editable
        )
        organization = left.text_input(
            "Organization", value=campaign.organization_name, disabled=not editable
        )
        address = right.text_input(
            "Organization/contact address",
            value=campaign.organization_address,
            disabled=not editable,
        )
        saved = st.form_submit_button(
            "Save composition", type="primary", width="stretch", disabled=not editable
        )
    if saved:
        try:
            configure_campaign(
                campaign,
                subject_template=subject,
                html_template=body,
                text_template=text_body,
                sender_name=sender_name,
                reply_to=reply_to,
                organization_name=organization,
                organization_address=address,
            )
            missing = validate_templates(recipients, subject, body, text_body)
            if missing:
                sample = next(iter(missing.items()))
                raise ValueError(
                    f"Missing values for {len(missing)} recipients; {sample[0]} lacks {', '.join(sample[1])}"
                )
            session.commit()
            st.success("Composition saved and every personalization token is resolvable.")
        except Exception as exc:
            session.rollback()
            st.error(str(exc))
    preview = st.selectbox(
        "Preview recipient",
        recipients,
        format_func=lambda item: f"{item.full_name or 'Unnamed'} · {item.email}",
        key=f"preview_{campaign.id}",
    )
    try:
        context = recipient_context(preview)
        preview_subject = render_template(subject, context, html_mode=False)
        preview_html = render_template(body, context, html_mode=True)
        st.markdown(f"**Subject:** {preview_subject}")
        st.components.v1.html(
            f"<div style='font:16px Georgia,serif;color:#18211f;padding:18px;background:#fffdf7'>{preview_html}</div>",
            height=230,
            scrolling=True,
        )
    except Exception as exc:
        st.error(f"Preview cannot render: {exc}")

    accounts = list(session.scalars(select(EmailAccount).where(EmailAccount.active.is_(True))).all())
    with st.expander("Send a test email"):
        test_email = st.text_input("Test recipient", key=f"test_email_{campaign.id}")
        if st.button("Send test now", disabled=not accounts, key=f"send_test_{campaign.id}"):
            try:
                if not normalize_email(test_email):
                    raise ValueError("Enter a valid test recipient email")
                context = recipient_context(preview)
                account = accounts[0]
                test_id = secrets.token_hex(20)
                message = build_email_message(
                    sender_email=account.email,
                    sender_name=campaign.sender_name,
                    recipient_email=test_email,
                    subject="[TEST] " + render_template(subject, context, html_mode=False),
                    text_body=render_template(text_body, context, html_mode=False),
                    html_body=render_template(body, context, html_mode=True),
                    reply_to=campaign.reply_to,
                    rfc_message_id=rfc_message_id(test_id),
                    unsubscribe_url=os.environ.get("PUBLIC_API_URL", "http://127.0.0.1:8000"),
                )
                GmailSender(account).send(message)
                st.success(f"Test accepted by Gmail for {test_email}.")
            except Exception as exc:
                st.error(str(exc))


def _schedule(session, campaign: Campaign) -> None:
    accounts = list(session.scalars(select(EmailAccount).where(EmailAccount.active.is_(True))).all())
    allowed_email = os.environ.get("ADMIN_EMAIL", "").strip().casefold()
    if allowed_email:
        accounts = [account for account in accounts if account.email.casefold() == allowed_email]
    if not accounts:
        st.warning("Connect Gmail before scheduling this campaign.")
        if st.button("Generate secure Gmail connection", type="primary"):
            try:
                st.session_state["gmail_authorization_url"] = build_gmail_authorization_url(session)
                session.commit()
            except Exception as exc:
                st.error(str(exc))
        url = st.session_state.get("gmail_authorization_url")
        if url:
            st.link_button("Continue with Google", str(url), type="primary", width="stretch")
        return
    account = st.selectbox(
        "Sending account", accounts, format_func=lambda item: item.email, key=f"account_{campaign.id}"
    )
    left, right = st.columns(2)
    start_date = left.date_input("Start date", value=datetime.now().date())
    start_time = right.time_input("Earliest start", value=time(10, 0))
    timezone = left.text_input("Timezone", value=campaign.timezone or "Asia/Kolkata")
    weekdays = right.multiselect(
        "Sending weekdays",
        options=list(range(7)),
        default=campaign.allowed_weekdays,
        format_func=lambda value: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][value],
    )
    hour_left, hour_right = st.columns(2)
    start_hour = hour_left.number_input(
        "Window starts", min_value=0, max_value=23, value=campaign.window_start_hour
    )
    end_hour = hour_right.number_input(
        "Window ends", min_value=1, max_value=24, value=campaign.window_end_hour
    )
    pace_left, pace_middle, pace_right = st.columns(3)
    interval_min = pace_left.number_input(
        "Minimum gap (minutes)", min_value=1, value=campaign.interval_min_minutes
    )
    interval_max = pace_middle.number_input(
        "Maximum gap (minutes)", min_value=1, value=campaign.interval_max_minutes
    )
    daily_target = pace_right.number_input(
        "Daily target",
        min_value=1,
        value=campaign.daily_target,
        help="No application maximum. Gmail may pause delivery when its own quota is reached.",
    )
    st.markdown(
        '<div class="ec-note ec-warning"><strong>No campaign-size cap.</strong> Gmail provider quota still applies. Quota-delayed recipients remain queued for a later eligible window.</div>',
        unsafe_allow_html=True,
    )
    confirmed = st.checkbox(
        "I reviewed the recipients, content, schedule, sender identity, and unsubscribe footer",
        key=f"schedule_confirm_{campaign.id}",
    )
    if st.button(
        "Schedule campaign",
        type="primary",
        width="stretch",
        disabled=not confirmed or campaign.status not in {"DRAFT", "PAUSED"},
    ):
        try:
            zone = ZoneInfo(timezone)
            start_at = datetime.combine(start_date, start_time, tzinfo=zone)
            schedule_campaign(
                session,
                campaign,
                account=account,
                start_at=start_at,
                schedule=CampaignSchedule(
                    timezone=timezone,
                    allowed_weekdays=tuple(weekdays),
                    window_start_hour=int(start_hour),
                    window_end_hour=int(end_hour),
                    interval_min_minutes=int(interval_min),
                    interval_max_minutes=int(interval_max),
                    daily_target=int(daily_target),
                ),
                public_api_url=os.environ.get("PUBLIC_API_URL", "http://127.0.0.1:8000"),
            )
            session.commit()
            st.success("Campaign is durably scheduled. The cloud worker can continue while this page is closed.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error(str(exc))


def _monitor(session, campaign: Campaign) -> None:
    metrics = campaign_metrics(session, campaign.id)
    st.markdown(
        f'<div class="ec-live"><strong>● {campaign.status}</strong><small>{campaign.name} · Gmail accepts a send before it is marked SENT</small></div>',
        unsafe_allow_html=True,
    )
    labels = [
        ("Recipients", "total"),
        ("Queued", "queued"),
        ("Sent", "sent"),
        ("Opened · estimated", "opened"),
        ("Clicked", "clicked"),
        ("Replied", "replied"),
        ("Bounced", "bounced"),
        ("Unsubscribed", "unsubscribed"),
    ]
    for start in (0, 4):
        columns = st.columns(4)
        for column, (label, key) in zip(columns, labels[start : start + 4]):
            column.metric(label, metrics[key])
    controls = st.columns(4)
    if controls[0].button("Refresh", width="stretch"):
        st.rerun()
    if controls[1].button(
        "Pause", width="stretch", disabled=campaign.status not in {"SCHEDULED", "RUNNING"}
    ):
        pause_campaign(campaign)
        session.commit()
        st.rerun()
    if controls[2].button(
        "Resume now", width="stretch", disabled=campaign.status != "PAUSED"
    ):
        resume_campaign(session, campaign, start_at=datetime.now(UTC))
        session.commit()
        st.rerun()
    if controls[3].button(
        "Cancel unsent", width="stretch", disabled=campaign.status in {"COMPLETED", "CANCELLED"}
    ):
        cancel_campaign(session, campaign)
        session.commit()
        st.rerun()

    recipients = recipients_frame(session, campaign.id)
    events = events_frame(session, campaign.id)
    recipient_tab, activity_tab, export_tab = st.tabs(["Recipients", "Activity ledger", "Exports"])
    with recipient_tab:
        if recipients.empty:
            st.info("No recipients are stored in this campaign.")
        else:
            editable_columns = ["Recipient ID", "Name", "Email", "Company", "Status", "Response", "Scheduled At", "Sent At", "Open Count", "Click Count", "Last Error"]
            edited = st.data_editor(
                recipients[[column for column in editable_columns if column in recipients.columns]],
                hide_index=True,
                width="stretch",
                disabled=[column for column in editable_columns if column != "Response"],
                column_config={
                    "Response": st.column_config.SelectboxColumn(
                        options=[item.value for item in ResponseClassification], required=True
                    )
                },
                key=f"campaign_recipients_{campaign.id}",
            )
            if st.button("Save response classifications", key=f"save_response_{campaign.id}"):
                for _, row in edited.iterrows():
                    recipient = session.get(CampaignRecipient, str(row["Recipient ID"]))
                    if recipient and recipient.response_classification != str(row["Response"]):
                        classify_response(
                            session, recipient, ResponseClassification(str(row["Response"]))
                        )
                session.commit()
                st.success("Response classifications and suppression decisions were saved.")
    with activity_tab:
        st.dataframe(events, hide_index=True, width="stretch")
    with export_tab:
        replies = recipients[recipients["Status"] == "REPLIED"] if not recipients.empty else recipients
        failures = (
            recipients[recipients["Status"].isin(["FAILED", "BOUNCED"])]
            if not recipients.empty
            else recipients
        )
        unsubscribes = (
            recipients[recipients["Status"] == "UNSUBSCRIBED"]
            if not recipients.empty
            else recipients
        )
        configuration = campaign_configuration_frame(campaign)
        st.download_button(
            "Download recipients CSV",
            recipients.to_csv(index=False).encode("utf-8-sig"),
            f"{campaign.name}_recipients.csv",
            "text/csv",
            width="stretch",
        )
        for label, frame, suffix in (
            ("Download replies CSV", replies, "replies"),
            ("Download failures CSV", failures, "failures"),
            ("Download unsubscribes CSV", unsubscribes, "unsubscribes"),
            ("Download configuration CSV", configuration, "configuration"),
        ):
            st.download_button(
                label,
                frame.to_csv(index=False).encode("utf-8-sig"),
                f"{campaign.name}_{suffix}.csv",
                "text/csv",
                width="stretch",
            )
        st.download_button(
            "Download activity CSV",
            events.to_csv(index=False).encode("utf-8-sig"),
            f"{campaign.name}_events.csv",
            "text/csv",
            width="stretch",
        )
        st.download_button(
            "Download campaign audit workbook",
            campaign_workbook(session, campaign),
            f"{campaign.name}_audit.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            width="stretch",
        )
        history = campaign_history_frame(session)
        st.download_button(
            "Download history for Outreach Intelligence",
            history.to_csv(index=False).encode("utf-8-sig"),
            "Campaign_Outreach_History.csv",
            "text/csv",
            width="stretch",
        )


_require_access()
st.markdown(
    """
    <div class="ec-hero"><div class="ec-kicker">Signal / Cloud email operations</div>
    <h1>Personal outreach,<br>paced like a human.</h1>
    <p>Turn verified prospects into durable Gmail campaigns. Preview every merge, schedule across working hours, and keep engagement and suppression evidence in one ledger.</p></div>
    <div class="ec-stages">
      <div class="ec-stage"><span>01 / ELIGIBILITY</span><b>Recipients</b></div>
      <div class="ec-stage"><span>02 / MESSAGE</span><b>Compose</b></div>
      <div class="ec-stage"><span>03 / DELIVERY</span><b>Schedule</b></div>
      <div class="ec-stage"><span>04 / SIGNAL</span><b>Monitor</b></div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Create a campaign from CSV / Excel", expanded=False):
    _create_from_upload()

with session_scope() as database_session:
    campaign = _selected_campaign(database_session)
    if campaign is None:
        st.info("Create a campaign here, or send safe leads from Outreach Intelligence.")
        st.stop()
    assert campaign is not None
    st.markdown(
        f'<div class="ec-band"><h2>{campaign.name}</h2><span class="ec-micro">{campaign.status} · {campaign.id[:8]}</span></div>',
        unsafe_allow_html=True,
    )
    recipient_tab, compose_tab, schedule_tab, monitor_tab = st.tabs(
        ["01 Recipients", "02 Compose", "03 Schedule", "04 Monitor"]
    )
    with recipient_tab:
        frame = recipients_frame(database_session, campaign.id)
        st.dataframe(frame, width="stretch", hide_index=True)
        st.caption(
            "Only valid, deduplicated, non-suppressed email addresses are stored. "
            "Campaign history is rechecked immediately before sending."
        )
    with compose_tab:
        _compose(database_session, campaign)
    with schedule_tab:
        _schedule(database_session, campaign)
    with monitor_tab:
        _monitor(database_session, campaign)
