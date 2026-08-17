from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from speedy_scraper.background_jobs import create_job, write_json
from speedy_scraper.outreach_job import run_outreach_job

PAGE = Path(__file__).parent.parent / "pages" / "3_Outreach_Intelligence.py"


def test_outreach_page_initial_state_is_ready_for_sources():
    app = AppTest.from_file(str(PAGE), default_timeout=15)

    app.run(timeout=15)

    assert not app.exception
    assert len(app.get("file_uploader")) == 2
    assert [button.label for button in app.button] == [
        "Load public Sheets",
        "Compare outreach history",
    ]
    assert app.button[1].disabled is True


def test_outreach_page_renders_completed_result_tabs_and_downloads(tmp_path: Path):
    primary_path = tmp_path / "primary.csv"
    previous_path = tmp_path / "previous.csv"
    pd.DataFrame(
        {
            "Name": ["Jane Doe", "New Person"],
            "Email": ["jane@example.com", "new@example.com"],
        }
    ).to_csv(primary_path, index=False)
    pd.DataFrame(
        {
            "POC": ["Jane Doe"],
            "Email": ["jane@example.com"],
            "Status": ["Contacted"],
        }
    ).to_csv(previous_path, index=False)
    job_dir = create_job(
        "outreach_intelligence", {}, jobs_root=tmp_path / "jobs"
    )
    write_json(
        job_dir / "config.json",
        {
            "default_phone_region": "IN",
            "status_map": {"Contacted": "CONTACTED"},
            "primary": {
                "source_id": "primary",
                "source_name": "primary.csv",
                "path": str(primary_path),
                "sheet_name": "CSV",
                "role": "PRIMARY",
                "mapping": {"full_name": "Name", "email": "Email"},
            },
            "previous": [
                {
                    "source_id": "previous",
                    "source_name": "previous.csv",
                    "path": str(previous_path),
                    "sheet_name": "CSV",
                    "role": "PREVIOUS",
                    "mapping": {
                        "full_name": "POC",
                        "email": "Email",
                        "status": "Status",
                    },
                }
            ],
        },
    )
    run_outreach_job(job_dir)
    app = AppTest.from_file(str(PAGE), default_timeout=20)
    app.session_state["oi_job_dir"] = str(job_dir)

    app.run(timeout=20)

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Fresh outreach",
        "Common people",
        "Review queue",
        "Combined master",
        "Rejected",
    ]
    assert [(metric.label, metric.value) for metric in app.metric[:5]] == [
        ("Primary prospects", "2"),
        ("Previous records", "1"),
        ("Unique people", "2"),
        ("Common people", "1"),
        ("Safe to contact", "1"),
    ]
    assert "Download complete Outreach Intelligence workbook" in [
        item.label for item in app.get("download_button")
    ]
