from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class RecipientStatus(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    OPENED = "OPENED"
    CLICKED = "CLICKED"
    REPLIED = "REPLIED"
    BOUNCED = "BOUNCED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED_SUPPRESSED = "SKIPPED_SUPPRESSED"


class ResponseClassification(StrEnum):
    UNKNOWN = "UNKNOWN"
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NO_RESPONSE = "NO_RESPONSE"
    RECONTACT_ALLOWED = "RECONTACT_ALLOWED"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"


class EventType(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    OPENED = "OPENED"
    CLICKED = "CLICKED"
    REPLIED = "REPLIED"
    BOUNCED = "BOUNCED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    FAILED = "FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    MANUAL_CLASSIFICATION = "MANUAL_CLASSIFICATION"


class Base(DeclarativeBase):
    pass


class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="gmail")
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    watch_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(32), default=CampaignStatus.DRAFT.value, index=True)
    email_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_accounts.id"), nullable=True
    )
    subject_template: Mapped[str] = mapped_column(Text, default="")
    html_template: Mapped[str] = mapped_column(Text, default="")
    text_template: Mapped[str] = mapped_column(Text, default="")
    sender_name: Mapped[str] = mapped_column(String(180), default="")
    reply_to: Mapped[str] = mapped_column(String(320), default="")
    organization_name: Mapped[str] = mapped_column(String(180), default="")
    organization_address: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Kolkata")
    allowed_weekdays: Mapped[list[int]] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4])
    window_start_hour: Mapped[int] = mapped_column(Integer, default=10)
    window_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    interval_min_minutes: Mapped[int] = mapped_column(Integer, default=4)
    interval_max_minutes: Mapped[int] = mapped_column(Integer, default=12)
    daily_target: Mapped[int] = mapped_column(Integer, default=100)
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reference: Mapped[str] = mapped_column(String(512), default="")
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[EmailAccount | None] = relationship()
    recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (
        Index("ix_campaign_recipient_due", "status", "scheduled_at"),
        Index("ix_campaign_recipient_email", "normalized_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320))
    full_name: Mapped[str] = mapped_column(String(240), default="")
    first_name: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(240), default="")
    designation: Mapped[str] = mapped_column(String(240), default="")
    linkedin_url: Mapped[str] = mapped_column(String(1000), default="")
    source_record_id: Mapped[str] = mapped_column(String(160), default="")
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=RecipientStatus.DRAFT.value, index=True)
    response_classification: Mapped[str] = mapped_column(
        String(32), default=ResponseClassification.UNKNOWN.value
    )
    rendered_subject: Mapped[str] = mapped_column(Text, default="")
    rendered_html: Mapped[str] = mapped_column(Text, default="")
    rendered_text: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    first_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    click_count: Mapped[int] = mapped_column(Integer, default=0)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(160))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(160), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")


class MessageEvent(Base):
    __tablename__ = "message_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    recipient_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_recipients.id"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    provider_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SuppressionEntry(Base):
    __tablename__ = "suppression_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    normalized_email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(120), default="campaign")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignLink(Base):
    __tablename__ = "campaign_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("campaign_recipients.id"), index=True)
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    destination_url: Mapped[str] = mapped_column(Text)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(120), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
