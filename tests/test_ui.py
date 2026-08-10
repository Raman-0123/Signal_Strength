from speedy_scraper.ui import action_button_css


def test_action_button_css_has_semantic_run_stop_and_recovery_states():
    css = action_button_css()
    assert "lead-run-action" in css
    assert "lead-stop-action" in css
    assert "lead-resume-action" in css
    assert "--action-run:#087d62" in css
    assert "--action-stop:#c43d2b" in css
    assert "--action-resume:#1769aa" in css
