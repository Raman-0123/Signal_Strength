from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from speedy_scraper.campaign_db import init_database, reset_engine_cache, session_scope
from speedy_scraper.email_campaigns import RecipientCandidate, configure_campaign, create_campaign

PAGE = Path(__file__).parent.parent / "pages" / "4_Email_Campaigns.py"


def test_email_campaign_page_initial_state_supports_upload(tmp_path, monkeypatch):
    reset_engine_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_SHA256", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("CAMPAIGN_SQLITE_PATH", str(tmp_path / "ui.db"))
    app = AppTest.from_file(str(PAGE), default_timeout=20)

    app.run(timeout=20)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == []
    assert len(app.get("file_uploader")) == 1
    assert "Create a campaign from CSV / Excel" in [item.label for item in app.expander]
    reset_engine_cache()


def test_render_blueprint_has_separate_web_api_worker_and_database():
    text = (PAGE.parent.parent / "render.yaml").read_text(encoding="utf-8")

    assert "signal-streamlit" in text
    assert "signal-campaign-api" in text
    assert "type: worker" in text
    assert "signal-campaign-db" in text
    assert "alembic upgrade head" in text


def test_email_campaign_page_renders_all_workflow_stages(tmp_path, monkeypatch):
    reset_engine_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_SHA256", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("CAMPAIGN_SQLITE_PATH", str(tmp_path / "campaign-ui.db"))
    init_database()
    with session_scope() as session:
        campaign, _ = create_campaign(
            session,
            name="Leadership invitation",
            candidates=[
                RecipientCandidate(
                    email="asha@example.com",
                    full_name="Asha Rao",
                    first_name="Asha",
                    company="Signal Labs",
                    designation="CIO",
                )
            ],
        )
        configure_campaign(
            campaign,
            subject_template="Hello {{first_name}}",
            html_template="<p>Hello {{first_name}}</p>",
            text_template="Hello {{first_name}}",
            sender_name="Raman",
            reply_to="raman@example.com",
            organization_name="Signal",
            organization_address="Mumbai",
        )
    app = AppTest.from_file(str(PAGE), default_timeout=20)

    app.run(timeout=20)

    assert not app.exception
    assert [tab.label for tab in app.tabs[:4]] == [
        "01 Recipients",
        "02 Compose",
        "03 Schedule",
        "04 Monitor",
    ]
    assert any(
        "Leadership invitation" in heading.value for heading in app.get("markdown")
    )
    assert "Download campaign audit workbook" in [
        button.label for button in app.get("download_button")
    ]
    reset_engine_cache()
