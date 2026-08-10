from __future__ import annotations

import re
import urllib.parse

_BAD_LINKEDIN_SLUGS = {
    "404",
    "company",
    "feed",
    "job",
    "jobs",
    "pub",
    "pulse",
    "posts",
    "school",
}


def normalize_linkedin_url(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    # Uploaded exports can contain arbitrary cells, including bracketed IP
    # literals such as ``[2001:db8::1]``. Python's URL parser rejects malformed
    # bracketed hosts; those values are not LinkedIn URLs and should fail
    # closed rather than aborting the worker.
    try:
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path
    except (TypeError, ValueError):
        return ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    match = re.match(r"^/(?:in|mwlite/profile/in)/([^/?#]+)", path, re.IGNORECASE)
    if not match:
        return ""
    slug = match.group(1).strip().strip("/").lower()
    if not slug or slug in _BAD_LINKEDIN_SLUGS:
        return ""
    if len(slug) > 100:
        return ""
    return f"https://www.linkedin.com/in/{slug}/"


def linkedin_id(value: str | None) -> str:
    url = normalize_linkedin_url(value)
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]
