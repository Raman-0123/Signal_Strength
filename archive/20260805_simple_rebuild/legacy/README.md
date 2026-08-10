# Archived runtime-excluded files

These files predate the persistent service architecture and are retained only
because this workspace has no Git history from which deleted files could be
recovered. They are not packaged, imported, or executed by Speedy-Scraper 2.x.

- `app.py.broken` and `app.py.bak`: superseded Streamlit entry points.
- `browser_scraper.py`: unused Playwright/Bing prototype.
- `engines_streamlit.py`: the pre-service, Streamlit-bound scraping engine,
  retained only for characterization coverage while the production runtime
  uses `speedy_scraper.engine`.
- `debug_playwright.py` and `debug_bing.html`: browser diagnostics.

The Google security-check fixture used by tests lives at
`tests/fixtures/debug_google.html`.
