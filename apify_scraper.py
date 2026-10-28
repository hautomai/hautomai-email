from __future__ import annotations

import json
from pathlib import Path

from apify_client import ApifyClient

from config import ROOT, config, env
import lead_quality

FIELD_CANDIDATES = {
    "email": ["emailDirect", "emailWork", "emailPersonal", "email", "contactEmail", "email_address"],
    "agency_name": ["companyName", "company_name", "company", "name"],
    "website": ["website", "websiteUrl", "companyWebsiteUrl", "company_website", "domain", "companyDomain", "company_domain"],
    "first": ["firstName", "personFirstName", "contactFirstName", "first_name"],
    "last": ["lastName", "personLastName", "contactLastName", "last_name"],
    "title": ["title", "personTitle", "jobTitle", "job_title", "professionalHeadline", "headline"],
    "size": ["companySize", "company_size", "employees", "employeeRange", "companySizeHumanized"],
    "linkedin": ["personLinkedInUrl", "linkedinUrl", "linkedin", "linkedin_url"],
}


def _pick(item: dict, keys: list[str]) -> str:
    lowered = {k.lower(): v for k, v in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value:
            return str(value).strip()
    return ""


def normalize(item: dict) -> dict:
    first = _pick(item, FIELD_CANDIDATES["first"])
    last = _pick(item, FIELD_CANDIDATES["last"])
    name = " ".join(x for x in (first, last) if x).strip()
    notes_parts = []
    size = _pick(item, FIELD_CANDIDATES["size"])
    if size:
        notes_parts.append(f"size={size}")
    linkedin = _pick(item, FIELD_CANDIDATES["linkedin"])
    if linkedin:
        notes_parts.append(f"linkedin={linkedin}")
    return {
        "agency_name": _pick(item, FIELD_CANDIDATES["agency_name"]),
        "contact_name": name,
        "contact_title": _pick(item, FIELD_CANDIDATES["title"]),
        "email": _pick(item, FIELD_CANDIDATES["email"]).lower(),
        "website": _pick(item, FIELD_CANDIDATES["website"]),
        "status": "new",
        "country": _pick(item, ["companyCountry", "country"]),
        "notes": "; ".join(notes_parts),
    }


def actor_configs() -> list[dict]:
    apify = config["apify"]
    actors = [{"actor_id": apify["actor_id"], "actor_input": dict(apify.get("actor_input", {}))}]
    actors.extend(apify.get("additional_actors", []) or [])
    return actors


def actor_info(client: ApifyClient) -> list[dict]:
    infos = []
    for actor_cfg in actor_configs():
        actor = client.actor(actor_cfg["actor_id"]).get()
        infos.append({
            "id": getattr(actor, "id", ""),
            "name": getattr(actor, "name", ""),
            "title": getattr(actor, "title", ""),
            "description": (getattr(actor, "description", None) or "")[:300],
        })
    return infos


def _inject_countries(run_input: dict, countries: list[str]) -> None:
    if "contact_location" in run_input:
        run_input["contact_location"] = [c.lower() for c in countries]
    else:
        run_input["companyLocationCountryIncludes"] = countries
        run_input["personLocationCountryIncludes"] = countries


def _quality_config() -> dict:
    return config["apify"].get("quality") or {}


def _gate_enabled() -> bool:
    return bool(_quality_config().get("enabled", True))


def _site_timeout() -> int:
    q = _quality_config()
    return int(q.get("site_timeout_seconds", config["website_scout"].get("timeout_seconds", 10)))


def _run_single(client: ApifyClient, actor_id: str, run_input: dict) -> list[dict]:
    run = client.actor(actor_id).call(run_input=run_input)
    return fetch_run_dataset(client, run.id)


def _run_actor_with_gate(client: ApifyClient, actor_cfg: dict) -> list[dict]:
    """Scrape once, then fetch each lead's website and keep only the relevant
    subset (sites that actually show PPC / paid-media service signals).

    Non-confirmed leads (unreachable site, no PPC signal, no website) are
    dropped here, before email verification runs.
    """
    base_input = dict(actor_cfg["actor_input"])
    countries = config["apify"].get("country_include") or []
    timeout = _site_timeout()
    if countries:
        _inject_countries(base_input, countries)
    items = _run_single(client, actor_cfg["actor_id"], base_input)
    if not _gate_enabled():
        return items
    confirmed, report = lead_quality.qualify(items, timeout)
    print(
        f"  QUALITY actor={actor_cfg['id']} "
        f"confirmed={report['confirmed']}/{report['total']} "
        f"unreachable={report['site_unreachable']} nosignal={report['no_ppc_signal']}"
    )
    print(f"  -> keeping {len(confirmed)} relevant lead(s); dropping {report['total'] - report['confirmed']}")
    return confirmed


def run_scrape(client: ApifyClient, actor_input: dict | None = None) -> list[dict]:
    items = []
    for actor_cfg in actor_configs():
        cfg = {
            "id": actor_cfg["actor_id"],
            "actor_id": actor_cfg["actor_id"],
            "actor_input": dict(actor_input or actor_cfg["actor_input"]),
        }
        items.extend(_run_actor_with_gate(client, cfg))
    return items


def fetch_run_dataset(client: ApifyClient, run_id: str) -> list[dict]:
    run = client.run(run_id).get()
    dataset_id = run.default_dataset_id
    if not dataset_id:
        raise SystemExit(f"Scrape run failed: {run.status_message}")
    items = client.dataset(dataset_id).list_items().items
    _dump_raw(items)
    return [normalize(item) for item in items]


def _dump_raw(items: list[dict]) -> None:
    payload = json.dumps(items, indent=2, default=str)
    for path in (ROOT / "data" / "raw" / "latest.json", Path("/tmp/ppc_raw_latest.json")):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            return
        except OSError:
            continue
