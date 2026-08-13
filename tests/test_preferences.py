import pytest

from speedy_scraper.preferences import (
    DEFAULT_POC_RESULT_COLUMNS,
    load_poc_result_columns,
    save_poc_result_columns,
)


def test_poc_columns_default_to_the_four_core_fields(tmp_path):
    assert load_poc_result_columns(tmp_path / "preferences.json") == list(
        DEFAULT_POC_RESULT_COLUMNS
    )


def test_poc_columns_are_saved_and_normalized(tmp_path):
    path = tmp_path / "preferences.json"

    save_poc_result_columns(["LinkedIn URL", "Name", "Unknown", "Company"], path)

    assert load_poc_result_columns(path) == ["Name", "Company", "LinkedIn URL"]


def test_poc_columns_cannot_be_saved_empty(tmp_path):
    with pytest.raises(ValueError, match="At least one"):
        save_poc_result_columns([], tmp_path / "preferences.json")
