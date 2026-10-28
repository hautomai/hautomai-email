from __future__ import annotations

import ipaddress
import os
import re
import time
from email.utils import parseaddr

import httpx

from config import env

_SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_RESERVED_DOMAINS = {"localhost", "example.com", "example.org", "example.net", "example.invalid"}

_SITE_CACHE: dict[str, bool] = {}

VERIFIER_ACTOR = "account56/email-verifier"


def _syntax_valid(email: str) -> bool:
    if not email or len(email) > 254 or not _SYNTAX_RE.match(email):
        return False
    name, addr = parseaddr(email)
    return bool(addr) and addr == email


def domain_reachable(domain: str, timeout: float = 5.0) -> bool:
    """True when the domain serves a website (any HTTP response counts).

    Any transport error (timeout, NXDOMAIN, connection refused, TLS error)
    means "not reachable" and the lead should be skipped entirely.
    """
    domain = (domain or "").strip().strip(".")
    if not domain or domain in _RESERVED_DOMAINS:
        return False
    if domain in _SITE_CACHE:
        return _SITE_CACHE[domain]
    reachable = False
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        try:
            with httpx.Client(follow_redirects=False, timeout=timeout) as client:
                response = client.get(url)
            if response.status_code:
                reachable = True
                break
        except Exception:
            continue
    _SITE_CACHE[domain] = reachable
    return reachable


def _provider_verify(email: str, timeout: float) -> dict | None:
    """Verifalia API check (free tier: 25 verifications/day).

    Returns a verdict dict, or None when the provider is not configured /
    fails / times out (caller then falls back to Hunter.io, or "unknown").
    Verifalia's classification is final: only "Deliverable" is valid,
    anything else (Risky, Undeliverable, ...) is invalid.
    """
    sid = (env("VERIFALIA_SID") or "").strip()
    token = (env("VERIFALIA_XAUTH_TOKEN") or "").strip()
    if not sid or not token:
        return None
    url = "https://api.verifalia.com/v2.7/email-validations"
    try:
        with httpx.Client(auth=(sid, token), timeout=timeout) as client:
            response = client.post(url, json={"entries": [{"inputData": email}]})
            response.raise_for_status()
            body = response.json()
            overview = body.get("overview") or {}
            job_id = overview.get("id")
            if job_id is None:
                return None
            deadline = time.monotonic() + timeout + 10
            while time.monotonic() < deadline:
                status = overview.get("status")
                if isinstance(status, dict):
                    status = status.get("id")
                entries = (body.get("entries") or {}).get("data") or []
                if status == "Completed" and entries:
                    classification = entries[0].get("classification") or ""
                    if classification == "Deliverable":
                        return {"verdict": "valid", "reason": ""}
                    return {"verdict": "invalid", "reason": f"verifalia_{classification.lower()}"}
                time.sleep(1)
                job = client.get(f"{url}/{job_id}").json()
                overview = job.get("overview") or {}
                body = job
    except Exception:
        return None
    return None


def _provider_verify_hunter(email: str, timeout: float) -> dict | None:
    """Hunter.io Email Verifier fallback (free tier: 50 verifications/month).

    Returns a verdict dict, or None when the provider is not configured /
    fails / times out (caller then treats the email as "unknown").
    """
    key = (env("HUNTER_API_KEY") or "").strip()
    if not key:
        return None
    url = "https://api.hunter.io/v2/email-verifier"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, params={"email": email, "api_key": key})
            if response.status_code == 202:
                response = client.get(url, params={"email": email, "api_key": key})
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            status = (data.get("status") or "").lower()
            if status == "valid":
                return {"verdict": "valid", "reason": ""}
            if status == "invalid":
                return {"verdict": "invalid", "reason": "hunter_undeliverable"}
            return {"verdict": "unknown", "reason": f"hunter_{status or 'unknown'}"}
    except Exception:
        return None


def _provider_verify_apify(emails: list[str], actor_id: str) -> dict[str, dict]:
    """Batch email verification via the Apify Million Verifier actor.

    Runs one actor call with all emails and returns {email: verdict_dict}.
    Verdict mapping from the actor's output record:
      result "ok"      -> {"verdict": "valid",  "quality": quality}
      result "invalid" -> {"verdict": "invalid", "reason": "apify_<subresult>"}
      anything else (risky/unknown/error) -> unknown (kept, flagged)
    Emails with no returned record are treated as "unknown" (unverifiable),
    so a provider hiccup never blocks a send outright.
    """
    emails = [e.lower().strip() for e in emails if (e or "").strip()]
    emails = list(dict.fromkeys(emails))
    if not emails:
        return {}
    from apify_client import ApifyClient

    tokens = []
    for key in ("APIFY_TOKEN", "APIFY_TOKEN_BACKUP"):
        token = (env(key) or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    results: dict[str, dict] = {}
    for token in tokens:
        try:
            client = ApifyClient(token)
            run = client.actor(actor_id).call(run_input={"emails": emails})
            dataset_id = getattr(run, "default_dataset_id", None)
            items = client.dataset(dataset_id).list_items().items if dataset_id else []
        except Exception as exc:
            print(f"[apify] token {token[:12]}... failed: {type(exc).__name__}: {exc}")
            continue
        for item in items:
            email = (item.get("email") or "").lower().strip()
            if not email:
                continue
            result = (item.get("result") or "").lower()
            quality = (item.get("quality") or "").lower()
            reason = item.get("subresult") or ""
            if result == "ok":
                results[email] = {"verdict": "valid", "reason": "", "quality": quality}
            elif result == "invalid":
                results[email] = {"verdict": "invalid", "reason": f"apify_{reason or 'invalid'}", "quality": quality}
            else:
                results[email] = {"verdict": "unknown", "reason": f"apify_{result or reason or 'unknown'}", "quality": quality}
        if results or dataset_id:
            for email in emails:
                results.setdefault(email, {"verdict": "unknown", "reason": "apify_no_result", "quality": ""})
            return results
    return {}


def _local_gates(email: str, require_site: bool, site_timeout: float) -> dict | None:
    """Run FREE local gates (syntax / domain / site reachability).

    Returns an invalid verdict dict when the email fails a local gate,
    or None when it passes (caller then asks the provider).
    """
    if not _syntax_valid(email):
        return {"verdict": "invalid", "reason": "syntax", "quality": ""}
    domain = email.rsplit("@", 1)[1].strip(".")
    if not domain:
        return {"verdict": "invalid", "reason": "no_domain", "quality": ""}
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        return {"verdict": "invalid", "reason": "ip_address", "quality": ""}
    if domain in _RESERVED_DOMAINS or domain.endswith(".invalid"):
        return {"verdict": "invalid", "reason": "reserved_domain", "quality": ""}
    if require_site and not domain_reachable(domain, site_timeout):
        return {"verdict": "invalid", "reason": "site_down", "quality": ""}
    return None


def _provider_verify_single(email: str, timeout: float, method: str, fallback: bool) -> dict:
    """Legacy single-email provider path (Verifalia / Hunter) for backward-compat."""
    if method == "apify":
        return _provider_verify_apify([email], VERIFIER_ACTOR).get(
            email, {"verdict": "unknown", "reason": "apify_no_result", "quality": ""}
        )
    result = _provider_verify(email, timeout + 5)
    if result is not None:
        return result
    if fallback:
        result = _provider_verify_hunter(email, timeout + 5)
        if result is not None and result["verdict"] != "unknown":
            return result
    return {"verdict": "unknown", "reason": "verification_unavailable", "quality": ""}


def verify_batch(
    emails: list[str],
    timeout: float = 3.0,
    method: str = "apify",
    require_site: bool = False,
    site_timeout: float = 5.0,
    actor_id: str | None = None,
    batch_cap: int = 100,
    fallback: bool = False,
    **kwargs,
) -> dict[str, dict]:
    """Batch email verification.

    Order: local gates (syntax -> website reachability when `require_site`) ->
    provider (Apify Million Verifier by default).

    Returns {email: {"verdict": "valid"|"invalid"|"unknown", "reason", "quality"}}.
    Only "invalid" means do-not-send; provider hiccups yield "unknown".
    `batch_cap` bounds how many emails the provider is asked about in one call
    (insurance against cost spikes on a bad runaway day).
    """
    verdicts: dict[str, dict] = {}
    to_probe: list[str] = []
    for email in (emails or []):
        email = (email or "").strip().lower()
        if not email:
            continue
        gate = _local_gates(email, require_site, site_timeout)
        if gate is not None:
            verdicts[email] = gate
        else:
            to_probe.append(email)
    if not to_probe:
        return verdicts
    if method == "apify":
        actor_id = actor_id or VERIFIER_ACTOR
        capped = to_probe[: max(1, batch_cap)] if batch_cap else to_probe
        probed = _provider_verify_apify(capped, actor_id)
        for email in to_probe:
            verdicts[email] = probed.get(
                email, {"verdict": "unknown", "reason": "apify_no_result", "quality": ""}
            )
    else:
        for email in to_probe:
            verdicts[email] = _provider_verify_single(email, timeout, method, fallback)
    return verdicts


def verify(
    email: str,
    timeout: float = 3.0,
    provider: bool = False,
    require_site: bool = False,
    site_timeout: float = 5.0,
    fallback: bool = False,
    method: str = "apify",
    actor_id: str | None = None,
    batch_cap: int = 100,
) -> dict:
    """Single-email verification (delegates to verify_batch).

    Order: local gates (syntax -> website reachability when `require_site`) ->
    provider (Apify Million Verifier by default; Verifalia/Hunter retained as
    legacy when `method` is not "apify").

    Returns {"verdict": "valid"|"invalid"|"unknown", "reason": str, "quality": str}.
    Only "invalid" means do-not-send; provider hiccups yield "unknown".
    """
    email = (email or "").strip().lower()
    if not email:
        return {"verdict": "invalid", "reason": "empty", "quality": ""}
    batch = verify_batch(
        [email],
        timeout=timeout,
        method=method,
        require_site=require_site,
        site_timeout=site_timeout,
        actor_id=actor_id,
        batch_cap=batch_cap,
        fallback=fallback,
    )
    return batch.get(email, {"verdict": "unknown", "reason": "verification_unavailable", "quality": ""})


def clear_cache() -> None:
    _SITE_CACHE.clear()