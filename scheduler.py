from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config import config

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
UTC = ZoneInfo("UTC")

INFO_DEFAULTS = {"tz": "UTC", "currency": "$"}

SEND_VERIFICATION = {str(v).strip().lower() for v in config["sheets"].get("send_verification", ["good"])}


def _normalize_country(country: str | None) -> str:
    if not country:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", country.lower()).strip()


def country_info(country: str | None, key: str | None = None):
    normalized = _normalize_country(country)
    countries = config.get("countries") or {}
    if normalized:
        info = countries.get(normalized)
        if info is None:
            info = next(
                (candidate for name, candidate in countries.items() if name in normalized or normalized in name),
                None,
            )
        if info is not None:
            return info if key is None else info.get(key, INFO_DEFAULTS.get(key))
    return INFO_DEFAULTS if key is None else INFO_DEFAULTS.get(key)


def tz_for(country: str | None) -> ZoneInfo:
    return ZoneInfo(country_info(country, "tz"))


def currency_for(country: str | None) -> str:
    return country_info(country, "currency")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(ZoneInfo(config["outbound"]["timezone"]))


def local_now(country: str | None, now: datetime | None = None) -> datetime:
    now = now or now_utc()
    return now.astimezone(tz_for(country))


def in_window(country: str | None, now: datetime | None = None) -> bool:
    outbound = config["outbound"]
    local = local_now(country, now).time()
    start = datetime.strptime(outbound["window_start"], "%H:%M").time()
    end = datetime.strptime(outbound["window_end"], "%H:%M").time()
    if start <= end:
        return start <= local <= end
    return local >= start or local <= end


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, TS_FORMAT)
    except (TypeError, ValueError):
        return None


def plan(rows: list[dict], now: datetime | None = None, ignore_window: bool = False) -> list[dict]:
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    outbound = config["outbound"]
    today = now.date()

    sent_today = 0
    for row in rows:
        for key in ("first_sent_at", "followup_1_at"):
            ts = _parse_ts(row.get(key))
            if ts and ts.date() == today:
                sent_today += 1

    budget = max(0, outbound["daily_send_cap"] - sent_today)

    queue = []
    for row in rows:
        if row.get("status") != "outreach_sent":
            continue
        if (row.get("verification") or "").strip().lower() not in SEND_VERIFICATION:
            continue
        if row.get("reply_at") or row.get("reply_class") or row.get("followup_1_at"):
            continue
        sent = _parse_ts(row.get("first_sent_at"))
        if sent and (now.replace(tzinfo=None) - sent).days >= outbound["followup_days"][0]:
            if ignore_window or in_window(row.get("country"), now):
                queue.append({"kind": "followup", "row": row})

    for row in rows:
        if row.get("status") != "new":
            continue
        if ignore_window or in_window(row.get("country"), now):
            queue.append({"kind": "outreach", "row": row})

    queue.sort(key=lambda item: item["kind"] == "outreach")
    return queue[:budget]