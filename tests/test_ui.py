import json
import re
import urllib.request
from pathlib import Path

from speedy_scraper.ui import (
    action_button_css,
    download_gsheet,
    prepare_failed_search_retry,
)


def test_action_button_css_has_semantic_run_stop_and_recovery_states():
    css = action_button_css()
    assert "lead-run-action" in css
    assert "lead-stop-action" in css
    assert "lead-resume-action" in css
    assert "lead-network-retry-action" in css
    assert "lead-challenge-retry-action" in css
    assert "lead-verify-collected-action" in css
    assert "--action-run:#087d62" in css
    assert "--action-stop:#c43d2b" in css
    assert "--action-resume:#1769aa" in css


def test_lead_finder_container_keys_are_unique():
    source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    keys = re.findall(r'\.container\(key="([^"]+)"\)', source)

    assert len(keys) == len(set(keys))


def test_manual_retry_removes_legacy_fallback_sources(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "sources": ["google_browser", "bing_browser", "ddgs"],
                "max_queries": 80,
                "max_results_per_query": 30,
                "max_pages_per_query": 5,
                "exclude_terms": ["former"],
            }
        ),
        encoding="utf-8",
    )

    config = prepare_failed_search_retry(tmp_path, local_manual=True)

    assert config["sources"] == ["google_browser"]
    assert config["browser_headless"] is False
    assert config["google_manual_challenge_seconds"] == 60
    assert config["retry_failed_searches"] is True
    assert config["max_queries"] == 24
    assert config["max_results_per_query"] == 10
    assert config["max_pages_per_query"] == 1
    assert config["exclude_terms"] == ["former"]


def test_google_sheet_tab_is_saved_as_a_dedupe_input(tmp_path, monkeypatch):
    requested_urls: list[str] = []

    def fake_urlretrieve(url, target):
        requested_urls.append(url)
        target.write_text(
            "Name,LinkedIn URL\nAsha Rao,https://www.linkedin.com/in/asha-rao/\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)

    paths = download_gsheet(
        "https://docs.google.com/spreadsheets/d/example-sheet/edit?gid=42",
        tmp_path,
    )

    assert len(paths) == 1
    assert paths[0].endswith("gsheet_example-sheet_42.csv")
    assert requested_urls == [
        "https://docs.google.com/spreadsheets/d/example-sheet/export?format=csv&gid=42"
    ]
