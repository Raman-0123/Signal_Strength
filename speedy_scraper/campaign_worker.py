from __future__ import annotations

import argparse
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from speedy_scraper.campaign_db import session_scope
from speedy_scraper.campaign_models import (
    Campaign,
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
from speedy_scraper.email_campaigns import campaign_schedule, strategic_send_slots
from speedy_scraper.email_sender import (
    GmailSender,
    SenderAdapter,
    SenderError,
    build_email_message,
    persist_refreshed_credentials,
    rfc_message_id,
)

SenderFactory = Callable[[EmailAccount], SenderAdapter]
ACTIVE_CAMPAIGNS = {CampaignStatus.SCHEDULED.value, CampaignStatus.RUNNING.value}
ENGAGED_STATUSES = {
    RecipientStatus.SENT.value,
    RecipientStatus.OPENED.value,
    RecipientStatus.CLICKED.value,
    RecipientStatus.REPLIED.value,
}


def run_worker(*, once: bool = False, poll_seconds: float = 15.0) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    last_mailbox_sync = 0.0
    while True:
        processed = process_one_due(worker_id=worker_id)
        with session_scope() as session:
            recover_stale_leases(session)
            if time.monotonic() - last_mailbox_sync >= 60:
                sync_all_mailboxes(session)
                last_mailbox_sync = time.monotonic()
            reconcile_campaigns(session)
        if once:
            return
        if not processed:
            time.sleep(max(1.0, poll_seconds))


def claim_due_recipient(
    session: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> str | None:
    current = now or utcnow()
    statement = (
        select(CampaignRecipient)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .where(
            Campaign.status.in_(ACTIVE_CAMPAIGNS),
            CampaignRecipient.status == RecipientStatus.QUEUED.value,
            CampaignRecipient.scheduled_at <= current,
            or_(
                CampaignRecipient.leased_until.is_(None),
                CampaignRecipient.leased_until < current,
            ),
        )
        .order_by(CampaignRecipient.scheduled_at, CampaignRecipient.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    recipient = session.scalar(statement)
    if recipient is None:
        return None
    suppressed = session.scalar(
        select(SuppressionEntry.id).where(
            SuppressionEntry.normalized_email == recipient.normalized_email
        )
    )
    if suppressed:
        recipient.status = RecipientStatus.SKIPPED_SUPPRESSED.value
        recipient.last_error = "Suppressed before send"
        return None
    already_contacted = session.scalar(
        select(CampaignRecipient.id).where(
            CampaignRecipient.id != recipient.id,
            CampaignRecipient.normalized_email == recipient.normalized_email,
            CampaignRecipient.status.in_(ENGAGED_STATUSES | {RecipientStatus.SENDING.value}),
            CampaignRecipient.response_classification
            != ResponseClassification.RECONTACT_ALLOWED.value,
        )
    )
    if already_contacted:
        recipient.status = RecipientStatus.SKIPPED_SUPPRESSED.value
        recipient.last_error = "Already contacted by another campaign"
        return None
    recipient.status = RecipientStatus.SENDING.value
    recipient.lease_owner = worker_id
    recipient.leased_until = current + timedelta(minutes=10)
    recipient.attempt_count += 1
    campaign = session.get(Campaign, recipient.campaign_id)
    if campaign and campaign.status == CampaignStatus.SCHEDULED.value:
        campaign.status = CampaignStatus.RUNNING.value
    session.flush()
    return recipient.id


def process_one_due(
    *,
    worker_id: str,
    now: datetime | None = None,
    sender_factory: SenderFactory | None = None,
) -> bool:
    current = now or utcnow()
    with session_scope() as session:
        recipient_id = claim_due_recipient(session, worker_id=worker_id, now=current)
    if not recipient_id:
        return False
    with session_scope() as session:
        recipient = session.get(CampaignRecipient, recipient_id)
        if recipient is None:
            return False
        campaign = session.get(Campaign, recipient.campaign_id)
        account = session.get(EmailAccount, campaign.email_account_id) if campaign else None
        if campaign is None or account is None or not account.active:
            _fail_recipient(session, recipient, "Connected Gmail account is unavailable")
            return True
        unsubscribe_url = (
            os.environ.get("PUBLIC_API_URL", "http://127.0.0.1:8000").rstrip("/")
            + f"/unsubscribe/{recipient.unsubscribe_token}"
        )
        message_id = rfc_message_id(recipient.idempotency_key)
        message = build_email_message(
            sender_email=account.email,
            sender_name=campaign.sender_name,
            recipient_email=recipient.email,
            subject=recipient.rendered_subject,
            text_body=recipient.rendered_text,
            html_body=recipient.rendered_html,
            reply_to=campaign.reply_to,
            rfc_message_id=message_id,
            unsubscribe_url=unsubscribe_url,
        )
        factory = sender_factory or GmailSender
        try:
            sender = factory(account)
            # A retry first searches Sent for our deterministic Message-ID. This
            # closes the crash window between Gmail accepting a message and our
            # local transaction committing the provider response.
            existing = sender.find_sent_message(message_id) if recipient.attempt_count > 1 else None
            result = existing or sender.send(message)
            recipient.gmail_message_id = result.message_id
            recipient.gmail_thread_id = result.thread_id
            recipient.status = RecipientStatus.SENT.value
            recipient.sent_at = recipient.sent_at or current
            recipient.last_error = ""
            recipient.lease_owner = None
            recipient.leased_until = None
            session.add(
                MessageEvent(
                    campaign_id=recipient.campaign_id,
                    recipient_id=recipient.id,
                    event_type=EventType.SENT.value,
                    provider_event_id=f"gmail:{result.message_id}:sent",
                    metadata_json={"recovered": existing is not None},
                )
            )
            if isinstance(sender, GmailSender):
                persist_refreshed_credentials(session, account, sender)
        except SenderError as exc:
            _handle_send_error(session, campaign, account, recipient, exc, current)
        except Exception as exc:
            _fail_recipient(session, recipient, f"Unexpected sender failure: {str(exc)[:400]}")
    return True


def recover_stale_leases(session: Session, *, now: datetime | None = None) -> int:
    current = now or utcnow()
    recipients = list(
        session.scalars(
            select(CampaignRecipient).where(
                CampaignRecipient.status == RecipientStatus.SENDING.value,
                CampaignRecipient.leased_until < current,
            )
        ).all()
    )
    for recipient in recipients:
        recipient.status = RecipientStatus.QUEUED.value
        recipient.scheduled_at = current
        recipient.lease_owner = None
        recipient.leased_until = None
        recipient.last_error = "Recovered an expired worker lease; Sent is checked before retry"
    return len(recipients)


def reconcile_campaigns(session: Session) -> None:
    campaigns = list(session.scalars(select(Campaign).where(Campaign.status.in_(ACTIVE_CAMPAIGNS))))
    for campaign in campaigns:
        pending = session.scalar(
            select(CampaignRecipient.id)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status.in_(
                    [RecipientStatus.QUEUED.value, RecipientStatus.SENDING.value]
                ),
            )
            .limit(1)
        )
        if pending is None:
            campaign.status = CampaignStatus.COMPLETED.value


def sync_all_mailboxes(session: Session) -> None:
    accounts = list(session.scalars(select(EmailAccount).where(EmailAccount.active.is_(True))))
    for account in accounts:
        try:
            sync_gmail_history(session, account)
            renew_gmail_watch(session, account)
        except Exception:
            # Sending must keep working if reply synchronization is temporarily unavailable.
            continue


def sync_gmail_history(
    session: Session,
    account: EmailAccount,
    *,
    sender: GmailSender | None = None,
) -> int:
    client = sender or GmailSender(account)
    if not account.history_id:
        profile = client.service.users().getProfile(userId="me").execute()
        account.history_id = str(profile.get("historyId") or "") or None
        return 0
    processed = 0
    page_token: str | None = None
    latest_history_id = account.history_id
    while True:
        try:
            response = (
                client.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=account.history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                    maxResults=500,
                )
                .execute()
            )
        except Exception as exc:
            if int(getattr(getattr(exc, "resp", None), "status", 0) or 0) != 404:
                raise
            # Gmail expires old history cursors. Reset to the current cursor;
            # subsequent messages will continue from a known durable point.
            profile = client.service.users().getProfile(userId="me").execute()
            account.history_id = str(profile.get("historyId") or "") or None
            return processed
        for history in response.get("history") or []:
            for added in history.get("messagesAdded") or []:
                message_stub = added.get("message") or {}
                message_id = str(message_stub.get("id") or "")
                if message_id:
                    processed += _process_mailbox_message(
                        session, account, client, message_id
                    )
        latest_history_id = str(response.get("historyId") or latest_history_id)
        page_token = str(response.get("nextPageToken") or "") or None
        if not page_token:
            break
    account.history_id = latest_history_id
    persist_refreshed_credentials(session, account, client)
    return processed


def renew_gmail_watch(
    session: Session,
    account: EmailAccount,
    *,
    sender: GmailSender | None = None,
) -> None:
    topic = os.environ.get("GOOGLE_PUBSUB_TOPIC", "").strip()
    if not topic:
        return
    now = utcnow()
    if account.watch_expires_at and _aware(account.watch_expires_at) > now + timedelta(hours=24):
        return
    client = sender or GmailSender(account)
    response = (
        client.service.users()
        .watch(userId="me", body={"topicName": topic, "labelIds": ["INBOX"]})
        .execute()
    )
    account.history_id = str(response.get("historyId") or account.history_id)
    expiration_ms = int(response.get("expiration") or 0)
    if expiration_ms:
        account.watch_expires_at = datetime.fromtimestamp(expiration_ms / 1000, tz=UTC)
    persist_refreshed_credentials(session, account, client)


def _process_mailbox_message(
    session: Session,
    account: EmailAccount,
    sender: GmailSender,
    message_id: str,
) -> int:
    event_key = f"gmail:{message_id}:mailbox"
    if session.scalar(select(MessageEvent.id).where(MessageEvent.provider_event_id == event_key)):
        return 0
    message = (
        sender.service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Auto-Submitted"],
        )
        .execute()
    )
    thread_id = str(message.get("threadId") or "")
    recipient = session.scalar(
        select(CampaignRecipient).where(CampaignRecipient.gmail_thread_id == thread_id)
    )
    if recipient is None:
        return 0
    headers = {
        str(item.get("name") or "").casefold(): str(item.get("value") or "")
        for item in ((message.get("payload") or {}).get("headers") or [])
    }
    from_header = headers.get("from", "").casefold()
    subject = headers.get("subject", "").casefold()
    if account.email.casefold() in from_header:
        return 0
    is_bounce = any(
        marker in f"{from_header} {subject}"
        for marker in ("mailer-daemon", "postmaster", "delivery status notification", "undeliverable")
    )
    now = utcnow()
    if is_bounce:
        recipient.status = RecipientStatus.BOUNCED.value
        recipient.bounced_at = now
        event_type = EventType.BOUNCED
        if session.scalar(
            select(SuppressionEntry.id).where(
                SuppressionEntry.normalized_email == recipient.normalized_email
            )
        ) is None:
            session.add(
                SuppressionEntry(
                    normalized_email=recipient.normalized_email,
                    reason="bounce",
                    source=f"campaign:{recipient.campaign_id}",
                )
            )
    else:
        recipient.status = RecipientStatus.REPLIED.value
        recipient.replied_at = now
        event_type = EventType.REPLIED
    session.add(
        MessageEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=event_type.value,
            provider_event_id=event_key,
            metadata_json={"gmail_message_id": message_id, "subject": headers.get("subject", "")},
        )
    )
    return 1


def _handle_send_error(
    session: Session,
    campaign: Campaign,
    account: EmailAccount,
    recipient: CampaignRecipient,
    exc: SenderError,
    now: datetime,
) -> None:
    recipient.lease_owner = None
    recipient.leased_until = None
    recipient.last_error = str(exc)[:500]
    if exc.authentication_failed:
        account.active = False
        campaign.status = CampaignStatus.PAUSED.value
        recipient.status = RecipientStatus.QUEUED.value
        session.add(
            MessageEvent(
                campaign_id=recipient.campaign_id,
                recipient_id=recipient.id,
                event_type=EventType.RETRY_SCHEDULED.value,
                metadata_json={"reason": "gmail_reauthorization_required"},
            )
        )
    elif exc.retryable:
        delay = timedelta(hours=24) if exc.quota_limited else timedelta(
            minutes=min(120, 2 ** min(recipient.attempt_count, 7))
        )
        next_time = strategic_send_slots(
            now + delay,
            1,
            campaign_schedule(campaign),
            seed=f"{recipient.id}:{recipient.attempt_count}",
        )[0]
        recipient.status = RecipientStatus.QUEUED.value
        recipient.scheduled_at = next_time
        session.add(
            MessageEvent(
                campaign_id=recipient.campaign_id,
                recipient_id=recipient.id,
                event_type=EventType.RETRY_SCHEDULED.value,
                metadata_json={
                    "quota_limited": exc.quota_limited,
                    "next_attempt_at": next_time.isoformat(),
                },
            )
        )
    else:
        _fail_recipient(session, recipient, str(exc))


def _fail_recipient(session: Session, recipient: CampaignRecipient, message: str) -> None:
    recipient.status = RecipientStatus.FAILED.value
    recipient.last_error = message[:500]
    recipient.lease_owner = None
    recipient.leased_until = None
    session.add(
        MessageEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type=EventType.FAILED.value,
            metadata_json={"error": message[:500]},
        )
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Signal email campaign worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    args = parser.parse_args()
    run_worker(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
