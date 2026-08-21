from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from speedy_scraper.campaign_api import app
from speedy_scraper.campaign_db import init_database, reset_engine_cache, session_scope
from speedy_scraper.campaign_models import CampaignLink, CampaignRecipient, RecipientStatus
from speedy_scraper.email_campaigns import (
    RecipientCandidate,
    configure_campaign,
    create_campaign,
)


@pytest.fixture
def tracked_recipient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reset_engine_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CAMPAIGN_SQLITE_PATH", str(tmp_path / "api.db"))
    init_database()
    with session_scope() as session:
        campaign, _ = create_campaign(
            session,
            name="API test",
            candidates=[RecipientCandidate(email="asha@example.com", full_name="Asha")],
        )
        configure_campaign(
            campaign,
            subject_template="Hi",
            html_template="<p>Hello</p>",
            text_template="Hello",
            sender_name="Raman",
            reply_to="",
            organization_name="Signal",
            organization_address="Mumbai",
        )
        recipient = session.scalar(select(CampaignRecipient))
        recipient.status = RecipientStatus.SENT.value
        recipient.sent_at = datetime.now(UTC)
        link = CampaignLink(
            recipient_id=recipient.id,
            token="click-token",
            destination_url="https://example.com/welcome",
        )
        session.add(link)
        values = (recipient.id, recipient.tracking_token, recipient.unsubscribe_token)
    yield values
    reset_engine_cache()


def test_health_open_click_and_unsubscribe_are_durable(tracked_recipient):
    recipient_id, open_token, unsubscribe_token = tracked_recipient
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        opened = client.get(f"/track/open/{open_token}.gif")
        clicked = client.get("/track/click/click-token", follow_redirects=False)
        unsubscribed = client.post(f"/unsubscribe/{unsubscribe_token}")

    assert opened.status_code == 200
    assert opened.headers["content-type"] == "image/gif"
    assert clicked.status_code == 302
    assert clicked.headers["location"] == "https://example.com/welcome"
    assert unsubscribed.status_code == 200
    with session_scope() as session:
        recipient = session.get(CampaignRecipient, recipient_id)
        assert recipient.open_count == 1
        assert recipient.click_count == 1
        assert recipient.status == RecipientStatus.UNSUBSCRIBED.value


def test_invalid_tracking_tokens_do_not_disclose_recipients(tracked_recipient):
    with TestClient(app) as client:
        assert client.get("/track/open/missing.gif").status_code == 200
        response = client.get("/track/click/missing", follow_redirects=False)
        assert response.status_code == 404
        assert client.get("/unsubscribe/missing").status_code == 404
