from __future__ import annotations

from urllib.parse import urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

CASE_STUDY_PATHS = [
    "",
    "/case-studies",
    "/case-studies/",
    "/work",
    "/our-work",
    "/results",
    "/case-study",
]


def _bs_text(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def _fetch_one(url: str, timeout: int, max_chars: int) -> dict | None:
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return None
        text = trafilatura.extract(response.text) or _bs_text(response.text)
        if not text:
            return None
        return {"url": str(response.url), "text": text[:max_chars]}
    except (httpx.HTTPError, ValueError):
        return None


def _url_candidates(url: str) -> list[str]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    urls = [url]
    if url.startswith("https://"):
        urls.append("http://" + url[len("https://") :])
    return urls


def fetch_site(url: str, timeout: int = 12, max_chars: int = 4000) -> dict | None:
    for candidate in _url_candidates(url):
        site = _fetch_site_base(candidate, timeout, max_chars)
        if site:
            return site
    return None


def _is_index_listing(site: dict | None) -> bool:
    if not site:
        return True
    return "index of /" in site.get("text", "").lower()[:500]


def fetch_homepage(url: str, timeout: float = 8) -> dict | None:
    """Fetch only the homepage body text (single request, https then http).

    Fast path for bulk relevance checks; unlike fetch_site it does not walk
    case-study paths, so it succeeds/fails in one round trip per scheme.
    """
    candidates = _url_candidates(url)
    for candidate in candidates:
        try:
            response = httpx.get(
                candidate,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=timeout,
                follow_redirects=True,
            )
            if response.status_code != 200:
                continue
            text = trafilatura.extract(response.text) or _bs_text(response.text)
            if text:
                return {"url": str(response.url), "text": text[:4000]}
        except (httpx.HTTPError, ValueError):
            continue
    return None


def _fetch_site_base(url: str, timeout: int, max_chars: int) -> dict | None:
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return None
    if base.startswith("http://") or base.startswith("https://"):
        budget = timeout
        per_request = max(3, min(budget, 6))
        homepage = None
        for path in CASE_STUDY_PATHS:
            if budget <= 0 and homepage:
                break
            site = _fetch_one(base + path, per_request, max_chars)
            budget -= per_request
            if not site or _is_index_listing(site):
                continue
            if path == "":
                homepage = site
            else:
                return site
        return homepage
    return None
