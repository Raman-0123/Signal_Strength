from __future__ import annotations

import hmac
import html
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from speedy_scraper.campaign_db import init_database, session_scope
from speedy_scraper.campaign_models import (
    CampaignLink,
    CampaignRecipient,
    EventType,
    MessageEvent,
    RecipientStatus,
    utcnow,
)
from speedy_scraper.email_campaigns import suppress_recipient
from speedy_scraper.email_sender import complete_gmail_authorization

PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    yield


app = FastAPI(title="Signal Campaign API", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@app.get("/oauth/gmail/callback", response_class=HTMLResponse)
def gmail_callback(request: Request, state: str = "", error: str = "") -> HTMLResponse:
    if error:
        return HTMLResponse(_result_page("Gmail was not connected", error, success=False), 400)
    if not state:
        return HTMLResponse(_result_page("Gmail was not connected", "Missing OAuth state", False), 400)
    try:
        with session_scope() as session:
            account = complete_gmail_authorization(
                session,
                state=state,
                authorization_response=str(request.url),
            )
            address = account.email
    except Exception as exc:
        return HTMLResponse(
            _result_page("Gmail was not connected", str(exc)[:400], success=False), 400
        )
    return HTMLResponse(
        _result_page("Gmail connected", f"{address} is ready for scheduled sending.", True)
    )


@app.get("/track/open/{token}.gif")
def track_open(token: str, request: Request) -> Response:
    with session_scope() as session:
        recipient = session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.tracking_token == token)
        )
        if recipient is not None:
            now = utcnow()
            recipient.open_count += 1
            recipient.first_opened_at = recipient.first_opened_at or now
            recipient.last_opened_at = now
            if recipient.status == RecipientStatus.SENT.value:
                recipient.status = RecipientStatus.OPENED.value
            session.add(
                MessageEvent(
                    campaign_id=recipient.campaign_id,
                    recipient_id=recipient.id,
                    event_type=EventType.OPENED.value,
                    metadata_json={
                        "estimated": True,
                        "user_agent": request.headers.get("user-agent", "")[:240],
                    },
                )
            )
    return Response(
        PIXEL_GIF,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get("/track/click/{token}")
def track_click(token: str, request: Request) -> RedirectResponse:
    with session_scope() as session:
        link = session.scalar(select(CampaignLink).where(CampaignLink.token == token))
        if link is None:
            raise HTTPException(status_code=404, detail="Tracking link not found")
        recipient = session.get(CampaignRecipient, link.recipient_id)
        if recipient is None:
            raise HTTPException(status_code=404, detail="Recipient not found")
        now = utcnow()
        recipient.click_count += 1
        recipient.first_clicked_at = recipient.first_clicked_at or now
        recipient.last_clicked_at = now
        if recipient.status in {RecipientStatus.SENT.value, RecipientStatus.OPENED.value}:
            recipient.status = RecipientStatus.CLICKED.value
        session.add(
            MessageEvent(
                campaign_id=recipient.campaign_id,
                recipient_id=recipient.id,
                event_type=EventType.CLICKED.value,
                metadata_json={
                    "destination": link.destination_url,
                    "user_agent": request.headers.get("user-agent", "")[:240],
                },
            )
        )
        destination = link.destination_url
    return RedirectResponse(destination, status_code=302)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str) -> HTMLResponse:
    with session_scope() as session:
        recipient = session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.unsubscribe_token == token)
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="Unsubscribe link not found")
        if recipient.status == RecipientStatus.UNSUBSCRIBED.value:
            return HTMLResponse(_unsubscribe_result("You are already unsubscribed."))
    return HTMLResponse(
        """
        <!doctype html><html><head><meta name="viewport" content="width=device-width">
        <title>Unsubscribe</title></head><body style="font-family:Georgia,serif;background:#f4f1e8;color:#18211f;padding:4rem">
        <main style="max-width:36rem;margin:auto;border:1px solid #18211f;background:#fffdf7;padding:2rem;box-shadow:7px 7px 0 #18211f">
        <p style="color:#087d62;letter-spacing:.12em;text-transform:uppercase">Signal outreach</p>
        <h1>Stop future email?</h1><p>This address will be added to the permanent suppression list.</p>
        <form method="post"><button style="background:#b64838;color:white;border:0;padding:.8rem 1.2rem;font-weight:bold">Unsubscribe</button></form>
        </main></body></html>
        """
    )


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(token: str) -> HTMLResponse:
    with session_scope() as session:
        recipient = session.scalar(
            select(CampaignRecipient).where(CampaignRecipient.unsubscribe_token == token)
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="Unsubscribe link not found")
        if recipient.status != RecipientStatus.UNSUBSCRIBED.value:
            suppress_recipient(session, recipient)
    return HTMLResponse(_unsubscribe_result("You have been unsubscribed."))


@app.post("/gmail/notifications", status_code=204)
async def gmail_notification(request: Request, token: str = "") -> Response:
    expected = os.environ.get("GOOGLE_PUBSUB_VERIFICATION_TOKEN", "").strip()
    if expected and not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid notification token")
    # Gmail notifications are intentionally only a wake-up hint. The durable
    # worker reads users.history from the last committed history ID, so repeated
    # or out-of-order Pub/Sub deliveries cannot lose or duplicate reply events.
    await request.body()
    return Response(status_code=204)


def _result_page(title: str, message: str, success: bool) -> str:
    color = "#087d62" if success else "#b64838"
    app_url = os.environ.get("PUBLIC_APP_URL", "http://localhost:8501/Email_Campaigns")
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_app_url = html.escape(app_url, quote=True)
    return f"""
    <!doctype html><html><head><meta name="viewport" content="width=device-width">
    <title>{safe_title}</title></head><body style="font-family:Georgia,serif;background:#f4f1e8;color:#18211f;padding:4rem">
    <main style="max-width:42rem;margin:auto;border:1px solid #18211f;background:#fffdf7;padding:2rem;box-shadow:7px 7px 0 #18211f">
    <p style="color:{color};letter-spacing:.12em;text-transform:uppercase">Signal / Gmail</p>
    <h1>{safe_title}</h1><p>{safe_message}</p><p><a href="{safe_app_url}" style="color:#087d62">Return to Email Campaigns</a></p>
    </main></body></html>
    """


def _unsubscribe_result(message: str) -> str:
    return f"""
    <!doctype html><html><head><meta name="viewport" content="width=device-width">
    <title>Unsubscribed</title></head><body style="font-family:Georgia,serif;background:#f4f1e8;color:#18211f;padding:4rem">
    <main style="max-width:36rem;margin:auto;border:1px solid #18211f;background:#fffdf7;padding:2rem;box-shadow:7px 7px 0 #18211f">
    <p style="color:#087d62;letter-spacing:.12em;text-transform:uppercase">Preference saved</p><h1>{message}</h1>
    <p>No future Signal campaign will send to this email address.</p></main></body></html>
    """
