from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from speedy_scraper.campaign_models import EmailAccount, OAuthState, utcnow

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


@dataclass(frozen=True)
class SendResult:
    message_id: str
    thread_id: str


class SenderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        quota_limited: bool = False,
        authentication_failed: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.quota_limited = quota_limited
        self.authentication_failed = authentication_failed


class SenderAdapter(Protocol):
    def send(self, message: EmailMessage) -> SendResult: ...

    def find_sent_message(self, rfc_message_id: str) -> SendResult | None: ...


def credential_cipher() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip().encode()
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required for Gmail credentials")
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY must be a Fernet key; generate one with Fernet.generate_key()"
        ) from exc


def encrypt_credentials(value: str) -> str:
    return credential_cipher().encrypt(value.encode()).decode()


def decrypt_credentials(value: str) -> str:
    try:
        return credential_cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored Gmail credentials cannot be decrypted") from exc


def gmail_client_config() -> dict[str, object]:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_OAUTH_REDIRECT_URI are required"
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def build_gmail_authorization_url(session: Session) -> str:
    from google_auth_oauthlib.flow import Flow

    state = secrets.token_urlsafe(36)
    session.add(OAuthState(state=state, expires_at=utcnow() + timedelta(minutes=15)))
    flow = Flow.from_client_config(
        gmail_client_config(), scopes=GMAIL_SCOPES, state=state
    )
    flow.redirect_uri = os.environ["GOOGLE_OAUTH_REDIRECT_URI"].strip()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return authorization_url


def complete_gmail_authorization(
    session: Session,
    *,
    state: str,
    authorization_response: str,
) -> EmailAccount:
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

    stored = session.get(OAuthState, state)
    now = utcnow()
    if stored is None or stored.used_at is not None or _aware(stored.expires_at) <= now:
        raise ValueError("OAuth state is invalid, expired, or already used")
    flow = Flow.from_client_config(
        gmail_client_config(), scopes=GMAIL_SCOPES, state=state
    )
    flow.redirect_uri = os.environ["GOOGLE_OAUTH_REDIRECT_URI"].strip()
    flow.fetch_token(authorization_response=authorization_response)
    credentials = flow.credentials
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    email = str(profile.get("emailAddress") or "").strip().casefold()
    if not email:
        raise ValueError("Google did not return the connected Gmail address")
    allowed_email = os.environ.get("ADMIN_EMAIL", "").strip().casefold()
    if allowed_email and email != allowed_email:
        raise ValueError(f"This deployment only allows the Gmail account {allowed_email}")
    encrypted = encrypt_credentials(credentials.to_json())
    account = session.scalar(select(EmailAccount).where(EmailAccount.email == email))
    if account is None:
        account = EmailAccount(
            email=email,
            encrypted_credentials=encrypted,
            history_id=str(profile.get("historyId") or "") or None,
        )
        session.add(account)
    else:
        account.encrypted_credentials = encrypted
        account.history_id = str(profile.get("historyId") or "") or account.history_id
        account.active = True
    stored.used_at = now
    session.flush()
    return account


class GmailSender:
    def __init__(self, account: EmailAccount):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        info = json.loads(decrypt_credentials(account.encrypted_credentials))
        credentials = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
        self.credentials = credentials
        self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def send(self, message: EmailMessage) -> SendResult:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        try:
            response = (
                self.service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
        except Exception as exc:
            raise _sender_error(exc) from exc
        message_id = str(response.get("id") or "")
        if not message_id:
            raise SenderError("Gmail accepted no message ID", retryable=True)
        return SendResult(message_id=message_id, thread_id=str(response.get("threadId") or ""))

    def find_sent_message(self, rfc_message_id: str) -> SendResult | None:
        query = f"in:sent rfc822msgid:{rfc_message_id}"
        try:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=1)
                .execute()
            )
        except Exception as exc:
            raise _sender_error(exc) from exc
        messages = response.get("messages") or []
        if not messages:
            return None
        item = messages[0]
        return SendResult(message_id=str(item.get("id") or ""), thread_id=str(item.get("threadId") or ""))


def build_email_message(
    *,
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    reply_to: str,
    rfc_message_id: str,
    unsubscribe_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = recipient_email
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
    message["Message-ID"] = rfc_message_id
    message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def rfc_message_id(idempotency_key: str) -> str:
    return f"<{idempotency_key}@signal-outreach.local>"


def persist_refreshed_credentials(session: Session, account: EmailAccount, sender: GmailSender) -> None:
    if sender.credentials.valid and sender.credentials.to_json():
        account.encrypted_credentials = encrypt_credentials(sender.credentials.to_json())


def _sender_error(exc: Exception) -> SenderError:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    text = re.sub(r"\s+", " ", str(exc))[:500]
    lower = text.casefold()
    quota = status == 429 or any(
        marker in lower
        for marker in ("dailylimitexceeded", "userratelimitexceeded", "quota")
    )
    authentication_failed = status == 401 or any(
        marker in lower for marker in ("invalid_grant", "unauthorized_client", "token has been")
    )
    retryable = quota or status >= 500 or status in {408, 409}
    return SenderError(
        text or "Gmail request failed",
        retryable=retryable,
        quota_limited=quota,
        authentication_failed=authentication_failed,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
