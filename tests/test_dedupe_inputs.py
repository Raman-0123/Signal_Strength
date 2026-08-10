from pathlib import Path

import pandas as pd

from speedy_scraper.pipeline import load_existing_urls


def test_load_existing_urls_scans_csv_and_all_excel_sheets(tmp_path: Path):
    csv_path = tmp_path / "existing.csv"
    pd.DataFrame(
        {
            "Name": ["Jane Doe", "Malformed row", "IP row"],
            "Profile URL": [
                "https://www.linkedin.com/in/jane-doe/?trk=abc",
                "https://[2001:db8::1",
                "192.0.2.10",
            ],
        }
    ).to_csv(csv_path, index=False)

    xlsx_path = tmp_path / "existing.xlsx"
    with pd.ExcelWriter(xlsx_path) as writer:
        pd.DataFrame(
            {
                "Name": ["Asha Rao"],
                "LinkedIn URL": ["https://linkedin.com/in/asha-rao/"],
            }
        ).to_excel(writer, sheet_name="Leads", index=False)
        pd.DataFrame(
            {
                "Person": ["Kabir Mehta"],
                "Contact": ["https://www.linkedin.com/in/kabir-mehta/"],
            }
        ).to_excel(writer, sheet_name="Archive", index=False)

    urls = load_existing_urls([csv_path, xlsx_path])

    assert "https://www.linkedin.com/in/jane-doe/" in urls
    assert "https://www.linkedin.com/in/asha-rao/" in urls
    assert "https://www.linkedin.com/in/kabir-mehta/" in urls
