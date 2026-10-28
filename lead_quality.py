from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import config
from website_scout import fetch_homepage

_DEFAULT_PPC_TERMS = [
    "PPC",
    "Pay per click",
    "Pay-per-click",
    "Google Ads",
    "Paid search",
    "Paid media",
    "Paid advertising",
    "Bing Ads",
    "Facebook Ads",
    "Meta Ads",
    "Media buying",
    "Performance marketing",
]


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower())


def _tt(terms: list[str]) -> list[str]:
    cleaned = set()
    for term in terms:
        normalized = " ".join(_clean(term).split())
        if normalized:
            cleaned.add(normalized)
    return sorted(cleaned, key=len, reverse=True)


def _ppc_terms() -> list[str]:
    quality = config["apify"].get("quality") or {}
    terms = quality.get("ppc_terms") or _DEFAULT_PPC_TERMS
    return _tt(terms) if isinstance(terms, list) else _tt(_DEFAULT_PPC_TERMS)


def classify_text(text: str) -> bool:
    """True when the text contains a PPC / paid-media service signal."""
    cleaned = _clean(text)
    if not cleaned:
        return False
    for term in _ppc_terms():
        if re.search(rf"(^|\s){re.escape(term)}(?=\s|$)", cleaned):
            return True
    return False


def check_item(item: dict, timeout: int) -> tuple[bool, str]:
    """(eligible, status). status: confirmed|no_ppc_signal|site_unreachable|no_website.

    Leads whose website can't be fetched are failed outright: they would be
    rejected by the email-verification site gate anyway, so an unreachable
    site means the lead is not usable.
    """
    website = (item.get("website") or "").strip()
    if not website:
        return False, "no_website"
    site = fetch_homepage(website, timeout=timeout)
    if site is None:
        return False, "site_unreachable"
    if classify_text(site.get("text", "")):
        return True, "confirmed"
    return False, "no_ppc_signal"


def _max_workers() -> int:
    quality = config["apify"].get("quality") or {}
    return max(1, int(quality.get("max_workers", 10)))


def qualify(items: list[dict], timeout: int) -> tuple[list[dict], dict]:
    """Split scraped items into confirmed-eligible leads and the rest (parallel).

    Returns (confirmed, report) where report is a dict with total/confirmed/
    per-status counts/rate for CLI + heartbeat visibility.
    """
    results = []
    with ThreadPoolExecutor(max_workers=_max_workers()) as pool:
        futures = {pool.submit(check_item, item, timeout): item for item in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                ok, status = fut.result()
            except Exception:
                ok, status = False, "site_unreachable"
            results.append((ok, status, item))
    counts = {"no_website": 0, "site_unreachable": 0, "no_ppc_signal": 0, "confirmed": 0}
    confirmed = []
    for ok, status, item in results:
        counts[status] += 1
        if ok:
            confirmed.append(item)
    total = len(items)
    report = {
        "total": total,
        "confirmed": counts["confirmed"],
        "no_website": counts["no_website"],
        "site_unreachable": counts["site_unreachable"],
        "no_ppc_signal": counts["no_ppc_signal"],
        "rate": (counts["confirmed"] / total) if total else 0.0,
    }
    return confirmed, report