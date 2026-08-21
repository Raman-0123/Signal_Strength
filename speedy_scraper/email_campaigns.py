from __future__ import annotations

import hashlib
import io
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from bs4 import BeautifulSoup
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from speedy_scraper.campaign_models import (
    Campaign,
    CampaignLink,
    CampaignRecipient,
    CampaignStatus,
    EmailAccount,
    EventType,
    MessageEvent,
    RecipientStatus,
    ResponseClassification,
    SuppressionEntry,
    utcnow,
)
from speedy_scraper.outreach_intelligence import normalize_email

TOKEN_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
SAFE_DECISION = "SAFE TO CONTACT"
TERMINAL_RECIPIENT_STATUSES = {
    RecipientStatus.REPLIED.value,
    RecipientStatus.BOUNCED.value,
    RecipientStatus.UNSUBSCRIBED.value,
    RecipientStatus.CANCELLED.value,
    RecipientStatus.SKIPPED_SUPPRESSED.value,
}


@dataclass(frozen=True)
class RecipientCandidate:
    email: str
    full_name: str = ""
    first_name: str = ""
    company: str = ""
    designation: str = ""
    linkedin_url: str = ""
    source_record_id: str = ""
    source_data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectedRecipient:
    row_number: int
    email: str
    name: str
    reason: str


@dataclass(frozen=True)
class CampaignSchedule:
    timezone: str = "Asia/Kolkata"
    allowed_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    window_start_hour: int = 10
    window_end_hour: int = 17
    interval_min_minutes: int = 4
    interval_max_minutes: int = 12
    daily_target: int = 100

    def validate(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        if not self.allowed_weekdays or any(day not in range(7) for day in self.allowed_weekdays):
            raise ValueError("Choose at least one valid sending weekday")
        if not 0 <= self.window_start_hour <= 23:
            raise ValueError("Sending start hour must be between 0 and 23")
        if not 1 <= self.window_end_hour <= 24:
            raise ValueError("Sending end hour must be between 1 and 24")
        if self.window_start_hour >= self.window_end_hour:
            raise ValueError("Sending window must end after it starts")
        if self.interval_min_minutes < 1:
            raise ValueError("Minimum interval must be at least one minute")
        if self.interval_max_minutes < self.interval_min_minutes:
            raise ValueError("Maximum interval cannot be lower than the minimum")
        if self.daily_target < 1:
            raise ValueError("Daily target must be at least one; campaign size is unrestricted")


def sanitize_template_key(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")
    if not key:
        return "field"
    if key[0].isdigit():
        return f"field_{key}"
    return key


def recipient_context(candidate: RecipientCandidate | CampaignRecipient) -> dict[str, str]:
    source_data = dict(candidate.source_data or {})
    context: dict[str, str] = {}
    for key, value in source_data.items():
        normalized_key = sanitize_template_key(key)
        context.setdefault(normalized_key, "" if value is None else str(value))
    full_name = str(candidate.full_name or "").strip()
    first_name = str(candidate.first_name or "").strip() or full_name.split(" ", 1)[0]
    context.update(
        {
            "email": candidate.email,
            "name": full_name,
            "full_name": full_name,
            "first_name": first_name,
            "company": str(candidate.company or ""),
            "designation": str(candidate.designation or ""),
            "linkedin_url": str(candidate.linkedin_url or ""),
        }
    )
    return context


def template_tokens(*templates: str) -> set[str]:
    tokens: set[str] = set()
    for template in templates:
        value = template or ""
        remainder = TOKEN_PATTERN.sub("", value)
        if any(marker in remainder for marker in ("{{", "{%", "{#")):
            raise ValueError(
                "Templates support only simple placeholders such as {{first_name}}"
            )
        tokens.update(TOKEN_PATTERN.findall(value))
    return tokens


def render_template(template: str, context: Mapping[str, object], *, html_mode: bool) -> str:
    template_tokens(template)
    environment = SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=html_mode,
        keep_trailing_newline=True,
    )
    try:
        return environment.from_string(template).render(**context)
    except TemplateError as exc:
        raise ValueError(str(exc)) from exc


def validate_templates(
    candidates: Iterable[RecipientCandidate | CampaignRecipient],
    subject_template: str,
    html_template: str,
    text_template: str,
) -> dict[str, list[str]]:
    tokens = template_tokens(subject_template, html_template, text_template)
    missing: dict[str, list[str]] = {}
    for candidate in candidates:
        context = recipient_context(candidate)
        absent = sorted(token for token in tokens if not str(context.get(token, "")).strip())
        if absent:
            missing[candidate.email] = absent
    return missing


def candidates_from_frame(
    frame: pd.DataFrame,
    mapping: Mapping[str, str],
    *,
    confirmed_safe: bool = False,
) -> tuple[list[RecipientCandidate], list[RejectedRecipient]]:
    email_column = str(mapping.get("email") or "")
    if not email_column or email_column not in frame.columns:
        raise ValueError("Map a column containing recipient email addresses")
    decision_column = str(mapping.get("decision") or "")
    candidates: list[RecipientCandidate] = []
    rejected: list[RejectedRecipient] = []
    seen: set[str] = set()
    for index, row in frame.iterrows():
        row_number = int(index) + 2 if isinstance(index, int) else len(candidates) + len(rejected) + 2
        raw = {str(key): _cell(value) for key, value in row.to_dict().items()}
        email_value = _mapped(raw, mapping, "email")
        normalized = normalize_email(email_value)
        name = _mapped(raw, mapping, "full_name")
        if not normalized:
            rejected.append(RejectedRecipient(row_number, email_value, name, "invalid_or_blank_email"))
            continue
        if decision_column:
            decision = raw.get(decision_column, "").strip().upper()
            if decision != SAFE_DECISION:
                rejected.append(RejectedRecipient(row_number, email_value, name, "not_safe_to_contact"))
                continue
        elif not confirmed_safe:
            rejected.append(RejectedRecipient(row_number, email_value, name, "safe_status_not_confirmed"))
            continue
        if normalized in seen:
            rejected.append(RejectedRecipient(row_number, email_value, name, "duplicate_email_in_source"))
            continue
        seen.add(normalized)
        first_name = _mapped(raw, mapping, "first_name")
        if not name:
            last_name = _mapped(raw, mapping, "last_name")
            name = " ".join(part for part in (first_name, last_name) if part).strip()
        candidates.append(
            RecipientCandidate(
                email=normalized,
                full_name=name,
                first_name=first_name or name.split(" ", 1)[0],
                company=_mapped(raw, mapping, "company"),
                designation=_mapped(raw, mapping, "designation"),
                linkedin_url=_mapped(raw, mapping, "linkedin_url"),
                source_record_id=_mapped(raw, mapping, "record_id"),
                source_data=raw,
            )
        )
    return candidates, rejected


def create_campaign(
    session: Session,
    *,
    name: str,
    candidates: Iterable[RecipientCandidate],
    source_reference: str = "",
) -> tuple[Campaign, list[RejectedRecipient]]:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("Campaign name is required")
    suppressed = set(session.scalars(select(SuppressionEntry.normalized_email)).all())
    contacted = set(
        session.scalars(
            select(CampaignRecipient.normalized_email).where(
                CampaignRecipient.status.notin_(
                    [
                        RecipientStatus.CANCELLED.value,
                        RecipientStatus.FAILED.value,
                        RecipientStatus.SKIPPED_SUPPRESSED.value,
                    ]
                ),
                CampaignRecipient.response_classification
                != ResponseClassification.RECONTACT_ALLOWED.value,
            )
        ).all()
    )
    campaign = Campaign(name=cleaned_name, source_reference=source_reference)
    session.add(campaign)
    session.flush()
    rejected: list[RejectedRecipient] = []
    seen: set[str] = set()
    for row_number, candidate in enumerate(candidates, start=2):
        normalized = normalize_email(candidate.email)
        if not normalized:
            rejected.append(
                RejectedRecipient(row_number, candidate.email, candidate.full_name, "invalid_email")
            )
            continue
        if normalized in suppressed:
            rejected.append(
                RejectedRecipient(row_number, candidate.email, candidate.full_name, "suppressed")
            )
            continue
        if normalized in contacted:
            rejected.append(
                RejectedRecipient(
                    row_number,
                    candidate.email,
                    candidate.full_name,
                    "already_in_campaign_history",
                )
            )
            continue
        if normalized in seen:
            rejected.append(
                RejectedRecipient(
                    row_number,
                    candidate.email,
                    candidate.full_name,
                    "duplicate_email_in_campaign",
                )
            )
            continue
        seen.add(normalized)
        session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                email=candidate.email,
                normalized_email=normalized,
                full_name=candidate.full_name,
                first_name=candidate.first_name or candidate.full_name.split(" ", 1)[0],
                company=candidate.company,
                designation=candidate.designation,
                linkedin_url=candidate.linkedin_url,
                source_record_id=candidate.source_record_id,
                source_data=candidate.source_data,
                tracking_token=secrets.token_urlsafe(24),
                unsubscribe_token=secrets.token_urlsafe(24),
                idempotency_key=secrets.token_hex(20),
            )
        )
    session.flush()
    return campaign, rejected


def configure_campaign(
    campaign: Campaign,
    *,
    subject_template: str,
    html_template: str,
    text_template: str,
    sender_name: str,
    reply_to: str,
    organization_name: str,
    organization_address: str,
) -> None:
    if campaign.status != CampaignStatus.DRAFT.value:
        raise ValueError("Scheduled campaign content is immutable; duplicate it to make changes")
    if not subject_template.strip() or not html_template.strip():
        raise ValueError("Subject and email body are required")
    if not sender_name.strip() or not organization_name.strip() or not organization_address.strip():
        raise ValueError("Sender, organization, and mailing/contact address are required")
    if reply_to and not normalize_email(reply_to):
        raise ValueError("Reply-to address is invalid")
    campaign.subject_template = subject_template
    campaign.html_template = html_template
    campaign.text_template = text_template
    campaign.sender_name = sender_name.strip()
    campaign.reply_to = normalize_email(reply_to) if reply_to else ""
    campaign.organization_name = organization_name.strip()
    campaign.organization_address = organization_address.strip()


def schedule_campaign(
    session: Session,
    campaign: Campaign,
    *,
    account: EmailAccount,
    start_at: datetime,
    schedule: CampaignSchedule,
    public_api_url: str,
) -> None:
    schedule.validate()
    was_draft = campaign.status == CampaignStatus.DRAFT.value
    if campaign.status not in {CampaignStatus.DRAFT.value, CampaignStatus.PAUSED.value}:
        raise ValueError("Only draft or paused campaigns can be scheduled")
    if not campaign.subject_template.strip() or not campaign.html_template.strip():
        raise ValueError("Save the email composition before scheduling")
    recipient_statement = select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign.id
    )
    if not was_draft:
        recipient_statement = recipient_statement.where(
            CampaignRecipient.status == RecipientStatus.QUEUED.value
        )
    recipients = list(
        session.scalars(
            recipient_statement.order_by(CampaignRecipient.created_at, CampaignRecipient.id)
        ).all()
    )
    if not recipients:
        raise ValueError("Campaign has no eligible recipients")
    if campaign.status == CampaignStatus.DRAFT.value:
        missing = validate_templates(
            recipients,
            campaign.subject_template,
            campaign.html_template,
            campaign.text_template,
        )
        if missing:
            sample = ", ".join(
                f"{email}: {', '.join(tokens)}" for email, tokens in list(missing.items())[:3]
            )
            raise ValueError(f"Personalization values are missing ({sample})")
    base_url = public_api_url.rstrip("/")
    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ValueError("Public API URL must use HTTPS (localhost is allowed for development)")

    current = utcnow()
    effective_start = max(start_at.astimezone(UTC), current)
    slots = strategic_send_slots(effective_start, len(recipients), schedule, seed=campaign.id)
    campaign.email_account_id = account.id
    campaign.status = CampaignStatus.SCHEDULED.value
    campaign.scheduled_start_at = effective_start
    campaign.timezone = schedule.timezone
    campaign.allowed_weekdays = list(schedule.allowed_weekdays)
    campaign.window_start_hour = schedule.window_start_hour
    campaign.window_end_hour = schedule.window_end_hour
    campaign.interval_min_minutes = schedule.interval_min_minutes
    campaign.interval_max_minutes = schedule.interval_max_minutes
    campaign.daily_target = schedule.daily_target
    if was_draft:
        session.query(CampaignLink).filter(
            CampaignLink.recipient_id.in_([recipient.id for recipient in recipients])
        ).delete(synchronize_session=False)
    for recipient, slot in zip(recipients, slots):
        if was_draft or not recipient.rendered_subject:
            context = recipient_context(recipient)
            recipient.rendered_subject = render_template(
                campaign.subject_template, context, html_mode=False
            )
            raw_html = render_template(campaign.html_template, context, html_mode=True)
            recipient.rendered_text = render_template(
                campaign.text_template or _plain_text(raw_html), context, html_mode=False
            )
            recipient.rendered_html = _tracking_html(
                session, campaign, recipient, raw_html, base_url
            )
        recipient.scheduled_at = slot
        recipient.status = RecipientStatus.QUEUED.value
        recipient.lease_owner = None
        recipient.leased_until = None
        session.add(
            MessageEvent(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                event_type=EventType.QUEUED.value,
                metadata_json={"scheduled_at": slot.isoformat()},
            )
        )


def strategic_send_slots(
    start_at: datetime,
    count: int,
    schedule: CampaignSchedule,
    *,
    seed: str = "signal",
) -> list[datetime]:
    schedule.validate()
    if start_at.tzinfo is None:
        raise ValueError("Scheduled start must include a timezone")
    zone = ZoneInfo(schedule.timezone)
    cursor = _align_to_window(start_at.astimezone(zone), schedule)
    generator = random.Random(hashlib.sha256(seed.encode()).digest())
    slots: list[datetime] = []
    active_date: date | None = None
    sent_today = 0
    for _ in range(count):
        cursor = _align_to_window(cursor, schedule)
        if active_date != cursor.date():
            active_date = cursor.date()
            sent_today = 0
        if sent_today >= schedule.daily_target:
            cursor = _next_business_day(cursor, schedule)
            active_date = cursor.date()
            sent_today = 0
        slots.append(cursor.astimezone(UTC))
        sent_today += 1
        delay = generator.randint(schedule.interval_min_minutes, schedule.interval_max_minutes)
        cursor += timedelta(minutes=delay)
    return slots


def pause_campaign(campaign: Campaign) -> None:
    if campaign.status in {CampaignStatus.SCHEDULED.value, CampaignStatus.RUNNING.value}:
        campaign.status = CampaignStatus.PAUSED.value


def resume_campaign(session: Session, campaign: Campaign, *, start_at: datetime) -> None:
    if campaign.status != CampaignStatus.PAUSED.value:
        raise ValueError("Only a paused campaign can be resumed")
    schedule = campaign_schedule(campaign)
    pending = list(
        session.scalars(
            select(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == RecipientStatus.QUEUED.value,
            )
            .order_by(CampaignRecipient.scheduled_at, CampaignRecipient.id)
        ).all()
    )
    for recipient, slot in zip(
        pending, strategic_send_slots(start_at, len(pending), schedule, seed=campaign.id)
    ):
        recipient.scheduled_at = slot
    campaign.scheduled_start_at = start_at.astimezone(UTC)
    campaign.status = CampaignStatus.SCHEDULED.value


def cancel_campaign(session: Session, campaign: Campaign) -> None:
    if campaign.status in {CampaignStatus.COMPLETED.value, CampaignStatus.CANCELLED.value}:
        return
    campaign.status = CampaignStatus.CANCELLED.value
    session.query(CampaignRecipient).filter(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.status.in_(
            [RecipientStatus.DRAFT.value, RecipientStatus.QUEUED.value]
        ),
    ).update(
        {
            CampaignRecipient.status: RecipientStatus.CANCELLED.value,
            CampaignRecipient.leased_until: None,
            CampaignRecipient.lease_owner: None,
        },
        synchronize_session=False,
    )


def suppress_recipient(
    session: Session,
    recipient: CampaignRecipient,
    *,
    reason: str = "unsubscribe",
) -> None:
    existing = session.scalar(
        select(SuppressionEntry).where(
            SuppressionEntry.normalized_email == recipient.normalized_email
        )
    )
    if existing is None:
        session.add(
            SuppressionEntry(
                normalized_email=recipient.normalized_email,
                reason=reason,
                source=f"campaign:{recipient.campaign_id}",
            )
        )
    recipient.status = RecipientStatus.UNSUBSCRIBED.value
    recipient.response_classification = ResponseClassification.DO_NOT_CONTACT.value
    recipient.unsubscribed_at = recipient.unsubscribed_at or utcnow()
    session.add(
        MessageEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=EventType.UNSUBSCRIBED.value,
        )
    )


def classify_response(
    session: Session,
    recipient: CampaignRecipient,
    classification: ResponseClassification,
) -> None:
    recipient.response_classification = classification.value
    if classification == ResponseClassification.DO_NOT_CONTACT:
        suppress_recipient(session, recipient, reason="manual_do_not_contact")
    session.add(
        MessageEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=EventType.MANUAL_CLASSIFICATION.value,
            metadata_json={"classification": classification.value},
        )
    )


def campaign_schedule(campaign: Campaign) -> CampaignSchedule:
    return CampaignSchedule(
        timezone=campaign.timezone,
        allowed_weekdays=tuple(campaign.allowed_weekdays),
        window_start_hour=campaign.window_start_hour,
        window_end_hour=campaign.window_end_hour,
        interval_min_minutes=campaign.interval_min_minutes,
        interval_max_minutes=campaign.interval_max_minutes,
        daily_target=campaign.daily_target,
    )


def campaign_metrics(session: Session, campaign_id: str) -> dict[str, int]:
    counts = dict(
        session.execute(
            select(CampaignRecipient.status, func.count(CampaignRecipient.id))
            .where(CampaignRecipient.campaign_id == campaign_id)
            .group_by(CampaignRecipient.status)
        ).all()
    )
    total = sum(int(value) for value in counts.values())
    return {
        "total": total,
        "queued": int(counts.get(RecipientStatus.QUEUED.value, 0)),
        "sent": sum(
            int(counts.get(status.value, 0))
            for status in (
                RecipientStatus.SENT,
                RecipientStatus.OPENED,
                RecipientStatus.CLICKED,
                RecipientStatus.REPLIED,
            )
        ),
        "opened": sum(
            int(counts.get(status.value, 0))
            for status in (
                RecipientStatus.OPENED,
                RecipientStatus.CLICKED,
                RecipientStatus.REPLIED,
            )
        ),
        "clicked": int(counts.get(RecipientStatus.CLICKED.value, 0)),
        "replied": int(counts.get(RecipientStatus.REPLIED.value, 0)),
        "bounced": int(counts.get(RecipientStatus.BOUNCED.value, 0)),
        "unsubscribed": int(counts.get(RecipientStatus.UNSUBSCRIBED.value, 0)),
        "failed": int(counts.get(RecipientStatus.FAILED.value, 0)),
    }


def recipients_frame(session: Session, campaign_id: str) -> pd.DataFrame:
    recipients = list(
        session.scalars(
            select(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
            .order_by(CampaignRecipient.scheduled_at, CampaignRecipient.created_at)
        ).all()
    )
    return pd.DataFrame(
        [
            {
                "Recipient ID": item.id,
                "Name": item.full_name,
                "Email": item.email,
                "Company": item.company,
                "Designation": item.designation,
                "LinkedIn URL": item.linkedin_url,
                "Status": item.status,
                "Response": item.response_classification,
                "Scheduled At": _iso(item.scheduled_at),
                "Sent At": _iso(item.sent_at),
                "First Opened At": _iso(item.first_opened_at),
                "Last Opened At": _iso(item.last_opened_at),
                "Open Count": item.open_count,
                "First Clicked At": _iso(item.first_clicked_at),
                "Last Clicked At": _iso(item.last_clicked_at),
                "Click Count": item.click_count,
                "Replied At": _iso(item.replied_at),
                "Bounced At": _iso(item.bounced_at),
                "Unsubscribed At": _iso(item.unsubscribed_at),
                "Last Error": item.last_error,
                "Source Record ID": item.source_record_id,
            }
            for item in recipients
        ]
    )


def events_frame(session: Session, campaign_id: str) -> pd.DataFrame:
    events = list(
        session.scalars(
            select(MessageEvent)
            .where(MessageEvent.campaign_id == campaign_id)
            .order_by(MessageEvent.occurred_at)
        ).all()
    )
    return pd.DataFrame(
        [
            {
                "Event ID": event.id,
                "Recipient ID": event.recipient_id or "",
                "Event": event.event_type,
                "Occurred At": event.occurred_at.isoformat(),
                "Details": event.metadata_json,
            }
            for event in events
        ]
    )


def campaign_history_frame(session: Session) -> pd.DataFrame:
    recipients = list(
        session.scalars(
            select(CampaignRecipient).where(
                CampaignRecipient.status.notin_(
                    [
                        RecipientStatus.DRAFT.value,
                        RecipientStatus.QUEUED.value,
                        RecipientStatus.CANCELLED.value,
                        RecipientStatus.SKIPPED_SUPPRESSED.value,
                    ]
                )
            )
        ).all()
    )
    return pd.DataFrame(
        [
            {
                "POC": item.full_name,
                "Email": item.email,
                "Company": item.company,
                "Designation": item.designation,
                "LinkedIn URL": item.linkedin_url,
                "Status": (
                    "Do not contact"
                    if item.status
                    in {RecipientStatus.UNSUBSCRIBED.value, RecipientStatus.BOUNCED.value}
                    else "Contacted"
                ),
                "Response": item.response_classification,
                "Notes": f"Campaign {item.campaign_id}; {item.status}",
                "Contact Date": _iso(item.sent_at),
            }
            for item in recipients
        ]
    )


def campaign_workbook(session: Session, campaign: Campaign) -> bytes:
    recipients = recipients_frame(session, campaign.id)
    events = events_frame(session, campaign.id)
    if recipients.empty or "Status" not in recipients.columns:
        replies = failures = unsubscribes = recipients.copy()
    else:
        replies = recipients[recipients["Status"] == "REPLIED"]
        failures = recipients[recipients["Status"].isin(["FAILED", "BOUNCED"])]
        unsubscribes = recipients[recipients["Status"] == "UNSUBSCRIBED"]
    configuration = campaign_configuration_frame(campaign)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        recipients.to_excel(writer, sheet_name="Recipients", index=False)
        events.to_excel(writer, sheet_name="Message Events", index=False)
        replies.to_excel(writer, sheet_name="Replies", index=False)
        failures.to_excel(writer, sheet_name="Failures", index=False)
        unsubscribes.to_excel(writer, sheet_name="Unsubscribes", index=False)
        configuration.to_excel(writer, sheet_name="Configuration", index=False)
    return output.getvalue()


def campaign_configuration_frame(campaign: Campaign) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"Setting": "Campaign ID", "Value": campaign.id},
            {"Setting": "Name", "Value": campaign.name},
            {"Setting": "Status", "Value": campaign.status},
            {"Setting": "Timezone", "Value": campaign.timezone},
            {"Setting": "Daily target", "Value": campaign.daily_target},
            {"Setting": "Sending hours", "Value": f"{campaign.window_start_hour}:00–{campaign.window_end_hour}:00"},
            {"Setting": "Interval minutes", "Value": f"{campaign.interval_min_minutes}–{campaign.interval_max_minutes}"},
            {"Setting": "Source", "Value": campaign.source_reference},
        ]
    )


def campaign_query() -> Select[tuple[Campaign]]:
    return select(Campaign).order_by(Campaign.created_at.desc())


def _mapped(row: Mapping[str, str], mapping: Mapping[str, str], field: str) -> str:
    column = str(mapping.get(field) or "")
    return row.get(column, "").strip() if column else ""


def _cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _align_to_window(value: datetime, schedule: CampaignSchedule) -> datetime:
    cursor = value
    while True:
        if cursor.weekday() not in schedule.allowed_weekdays:
            cursor = _next_business_day(cursor, schedule)
            continue
        start = cursor.replace(
            hour=schedule.window_start_hour, minute=0, second=0, microsecond=0
        )
        end = cursor.replace(
            hour=0 if schedule.window_end_hour == 24 else schedule.window_end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if schedule.window_end_hour == 24:
            end += timedelta(days=1)
        if cursor < start:
            return start
        if cursor >= end:
            cursor = _next_business_day(cursor, schedule)
            continue
        return cursor


def _next_business_day(value: datetime, schedule: CampaignSchedule) -> datetime:
    cursor = (value + timedelta(days=1)).replace(
        hour=schedule.window_start_hour, minute=0, second=0, microsecond=0
    )
    while cursor.weekday() not in schedule.allowed_weekdays:
        cursor += timedelta(days=1)
    return cursor


def _tracking_html(
    session: Session,
    campaign: Campaign,
    recipient: CampaignRecipient,
    rendered_html: str,
    base_url: str,
) -> str:
    soup = BeautifulSoup(rendered_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        destination = str(anchor.get("href") or "").strip()
        parsed = urlparse(destination)
        if parsed.scheme not in {"http", "https"}:
            continue
        token = secrets.token_urlsafe(24)
        session.add(
            CampaignLink(
                recipient_id=recipient.id,
                token=token,
                destination_url=destination,
            )
        )
        anchor["href"] = f"{base_url}/track/click/{token}"
    footer = soup.new_tag("p")
    footer["style"] = "color:#66706d;font-size:12px;margin-top:28px"
    footer.append(
        f"Sent by {campaign.sender_name} · {campaign.organization_name} · "
        f"{campaign.organization_address} · "
    )
    unsubscribe = soup.new_tag("a", href=f"{base_url}/unsubscribe/{recipient.unsubscribe_token}")
    unsubscribe.string = "Unsubscribe"
    footer.append(unsubscribe)
    pixel = soup.new_tag(
        "img",
        src=f"{base_url}/track/open/{recipient.tracking_token}.gif",
        width="1",
        height="1",
        alt="",
    )
    pixel["style"] = "display:block;width:1px;height:1px;border:0"
    if soup.body:
        soup.body.append(footer)
        soup.body.append(pixel)
    else:
        soup.append(footer)
        soup.append(pixel)
    return str(soup)


def _plain_text(value: str) -> str:
    return BeautifulSoup(value, "html.parser").get_text("\n", strip=True)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""
