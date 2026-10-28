from __future__ import annotations

import time
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from config import config
from gmail_client import GmailClient, GoogleAuth, SHEETS_READ_LIMITER, _retry
from scheduler import TS_FORMAT, now_local
from sheets_crm import SheetsCrm

OPS_SHEET = "Ops"
OPS_COLUMNS = ["date", "component", "status", "detail", "at"]
OPS_LETTERS = "E"

OK_LABEL = "ok"
WARN_LABEL = "warn"
ERROR_LABEL = "error"

_META_CACHE: dict[str, tuple[float, list[str]]] = {}
_META_TTL_SECONDS = 30 * 60
_OPS_SERVICE = None

_ALERT_COOLDOWN_SECONDS = 30 * 60
_LAST_ALERT: dict[str, float] = {}


def _sheet_id() -> str:
    value = config["sheets"].get("spreadsheet_id") or ""
    if not value:
        raise SystemExit("Spreadsheet not set. Run: python main.py setup-sheet")
    return value


def _monitoring() -> dict:
    return config.get("monitoring", {}) or {}


def _service():
    global _OPS_SERVICE
    if _OPS_SERVICE is None:
        credentials = GoogleAuth.refresh(GoogleAuth.token(), persist=False)
        _OPS_SERVICE = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    return _OPS_SERVICE


def _limited_read(fn) -> dict:
    SHEETS_READ_LIMITER.wait()
    return _retry(fn)


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, TS_FORMAT)
    except (TypeError, ValueError):
        return None


def _ops_titles(spreadsheet_id: str) -> list[str]:
    cached = _META_CACHE.get(spreadsheet_id)
    if cached and time.monotonic() - cached[0] < _META_TTL_SECONDS:
        return cached[1]
    meta = _limited_read(
        lambda: _service().spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    )
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    _META_CACHE[spreadsheet_id] = (time.monotonic(), titles)
    return titles


def _read_ops(spreadsheet_id: str) -> list[dict]:
    if OPS_SHEET not in _ops_titles(spreadsheet_id):
        _retry(
            lambda: _service()
            .spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": OPS_SHEET}}}]},
            )
            .execute()
        )
        del _META_CACHE[spreadsheet_id]
    data = _limited_read(
        lambda: _service().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{OPS_SHEET}!A1:{OPS_LETTERS}"
        ).execute()
    )
    values = data.get("values", [])
    if not values or not values[0]:
        _retry(
            lambda: _service().spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{OPS_SHEET}!A1",
                valueInputOption="RAW",
                body={"values": [OPS_COLUMNS]},
            ).execute()
        )
        return []
    rows = []
    for idx, row in enumerate(values[1:], start=2):
        rows.append(dict(zip(OPS_COLUMNS, row)))
        rows[-1]["_row"] = idx
    return rows


def heartbeat(component: str, status: str, detail: str) -> None:
    spreadsheet_id = _sheet_id()
    now = now_local()
    date_key = now.strftime("%Y-%m-%d")
    at = now.strftime(TS_FORMAT)
    rows = _read_ops(spreadsheet_id)
    match = next(
        (r for r in rows if r.get("date") == date_key and r.get("component") == component),
        None,
    )
    if match:
        row = match["_row"]
        _retry(
            lambda: _service().spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": [
                        {"range": f"{OPS_SHEET}!C{row}", "values": [[status]]},
                        {"range": f"{OPS_SHEET}!D{row}", "values": [[detail]]},
                        {"range": f"{OPS_SHEET}!E{row}", "values": [[at]]},
                    ],
                },
            ).execute()
        )
    else:
        _retry(
            lambda: _service().spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{OPS_SHEET}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[date_key, component, status, detail, at]]},
            ).execute()
        )


def header_note(note: str) -> None:
    spreadsheet_id = _sheet_id()
    sheet_name = config["sheets"]["sheet_name"]
    note_range = config["sheets"].get("health_note_range") or "S1:T1"
    _retry(
        lambda: _service().spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!{note_range}",
            valueInputOption="RAW",
            body={"values": [["health", note]]},
        ).execute()
    )


def _recipient() -> str:
    recipient = (_monitoring().get("recipient") or "").strip()
    return recipient or GmailClient().my_email()


def alert(component: str, message: str) -> None:
    key = f"{component}|{message[:120]}"
    now = time.time()
    if now - _LAST_ALERT.get(key, 0) < _ALERT_COOLDOWN_SECONDS:
        return
    _LAST_ALERT[key] = now
    spreadsheet_id = _sheet_id()
    heartbeat(component, ERROR_LABEL, message[:240])
    header_note(f"[ERROR] {component}: {message[:90]}")
    body = (
        f"The {component} step of the outreach pipeline just failed.\n\n"
        f"Time: {now_local().strftime(TS_FORMAT)} (Europe/London)\n"
        f"Step: {component}\n"
        f"Error: {message}\n\n"
        "Next steps: 1) open the Ops tab of the CRM spreadsheet, "
        "2) check modal app logs (ppc-outreach), 3) fix and rerun."
    )
    GmailClient().send_email(_recipient(), f"[outreach] ERROR - {component} failed", body)


def _time_short(at: str) -> str:
    return at[11:16] if at else "-"


def build_report() -> dict:
    spreadsheet_id = _sheet_id()
    stamp = now_local()
    today = stamp.date()
    rows = _read_ops(spreadsheet_id)
    today_rows = [r for r in rows if r.get("date") == today.strftime("%Y-%m-%d")]

    def latest(component: str) -> dict | None:
        for row in reversed(today_rows):
            if row.get("component") == component:
                return row
        return None

    stale_after = int(_monitoring().get("poll_stale_after_minutes", 20) or 20)

    def component_status(component: str, stale_minutes: int | None = None) -> tuple[str, str, dict | None]:
        row = latest(component)
        if row is None:
            return ERROR_LABEL, "no run recorded today", None
        if row.get("status") == ERROR_LABEL:
            return ERROR_LABEL, row.get("detail") or "failed", row
        if stale_minutes:
            at = _parse_ts(row.get("at"))
            age = (stamp.replace(tzinfo=None) - at).total_seconds() / 60 if at else 99999
            if age > stale_minutes:
                return ERROR_LABEL, f"stale - last check {age:.0f} min ago", row
        return OK_LABEL, row.get("detail") or "ok", row

    lines = []
    issues = []

    outreach_status, outreach_detail, outreach_row = component_status("outreach")
    if outreach_status == ERROR_LABEL:
        issues.append(f"outreach: {outreach_detail[:80]}")
        if outreach_row is not None:
            lines.append(f"[ERROR] outreach {outreach_detail}")
        else:
            lines.append("[ERROR] outreach no run recorded today")
    else:
        lines.append(f"[OK]    outreach {outreach_detail} ({_time_short(outreach_row.get('at'))})")

    poll_status, poll_detail, poll_row = component_status("poll", stale_minutes=stale_after)
    if poll_status == ERROR_LABEL:
        issues.append(f"poll: {poll_detail[:80]}")
        if poll_row is not None:
            lines.append(f"[ERROR] poll    {poll_detail}")
        else:
            lines.append("[ERROR] poll    no check recorded - poll loop may be down")
    else:
        lines.append(f"[OK]    poll    {poll_detail} ({_time_short(poll_row.get('at'))})")

    crm = SheetsCrm(spreadsheet_id)
    crm.ensure_sheet()
    crm_rows = crm.rows()
    summary = crm.summary()

    sent_today = 0
    for row in crm_rows:
        for key in ("first_sent_at", "followup_1_at"):
            parsed = _parse_ts(row.get(key))
            if parsed and parsed.date() == today:
                sent_today += 1

    replies_today = []
    for row in crm_rows:
        parsed = _parse_ts(row.get("reply_at"))
        if parsed and parsed.date() == today:
            replies_today.append(row)

    reply_class_counts = {}
    for row in replies_today:
        cls = (row.get("reply_class") or "other").strip() or "other"
        reply_class_counts[cls] = reply_class_counts.get(cls, 0) + 1

    followup_days = int(config["outbound"].get("followup_days", [3])[0])
    due = 0
    for row in crm_rows:
        if row.get("status") != "outreach_sent":
            continue
        if row.get("followup_1_at") or row.get("reply_at"):
            continue
        sent = _parse_ts(row.get("first_sent_at"))
        if sent and (stamp.replace(tzinfo=None) - sent).days >= followup_days:
            due += 1

    funnel_parts = [f"{key}: {count}" for key, count in sorted(summary.items())]
    reply_parts = [f"{key} {count}" for key, count in sorted(reply_class_counts.items())]
    daily_cap = config["outbound"]["daily_send_cap"]

    status = ERROR_LABEL if issues else OK_LABEL
    if status == OK_LABEL:
        verdict_text = "HEALTH OK - all systems running"
    else:
        verdict_text = "ACTION REQUIRED - see issues above"

    body = "\n".join(
        [
            "OUTREACH PIPELINE - daily health report",
            f"Generated: {stamp.strftime('%Y-%m-%d %H:%M')} ({config['outbound']['timezone']})",
            "",
            "COMPONENTS",
        ]
        + lines
        + (["", "ISSUES"] + [f"- {issue}" for issue in issues] if issues else [])
        + [
            "",
            "CRM",
            f"Funnel: {(' | '.join(funnel_parts) if funnel_parts else 'empty')}",
            f"Sends today: {sent_today} of {daily_cap} cap | Follow-ups due: {due}",
            f"Replies today: {len(replies_today)}"
            + (f" ({', '.join(reply_parts)})" if reply_parts else ""),
            "",
            f"VERDICT: {verdict_text}",
        ]
    )

    short = {
        component: ("ok" if s == OK_LABEL else "ERROR") for component, s in
        (("outreach", outreach_status), ("poll", poll_status))
    }
    if outreach_status == ERROR_LABEL and outreach_row is None:
        short["outreach"] = "MISSING"
    if poll_status == ERROR_LABEL and poll_row is None:
        short["poll"] = "MISSING"
    top = "HEALTH OK" if status == OK_LABEL else "HEALTH ALERT"
    note = (
        f"{top} | outreach {short['outreach']}, poll {short['poll']}"
        f" | {stamp.strftime('%Y-%m-%d %H:%M')} local"
    )

    if status == OK_LABEL:
        subject = "[outreach] OK - all systems running"
    else:
        subject = f"[outreach] ALERT - {len(issues)} issue(s) need attention"

    return {
        "status": status,
        "subject": subject,
        "body": body,
        "note": note,
        "lines": lines,
        "issues": issues,
    }


def run_daily_check(send: bool = True) -> dict:
    report = build_report()
    header_note(report["note"])
    detail = "no issues" if report["status"] == OK_LABEL else f"{len(report['issues'])} issue(s)"
    heartbeat("monitor", report["status"], detail)
    if send:
        recipient = _recipient()
        print(f"Sending daily digest to {recipient}")
        GmailClient().send_email(recipient, report["subject"], report["body"])
    else:
        print("Digest built (not sent). Use --send to email it.")
    return report


def verify_header_note() -> str:
    note_range = config["sheets"].get("health_note_range") or "S1:T1"
    data = _retry(
        lambda: _service().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range=f"{config['sheets']['sheet_name']}!{note_range}"
        ).execute()
    )
    values = data.get("values", [])
    if not values or not values[0]:
        return ""
    return values[0][1] if len(values[0]) > 1 else values[0][0]


def verify_ops() -> list[dict]:
    return _read_ops(_sheet_id())


def ran_today(*components: str) -> set[str]:
    """Which of `components` already recorded a run today.

    The Ops tab holds exactly one row per component per day (`heartbeat`
    upserts), so a row's presence is a reliable once-a-day marker: the daily
    jobs share the single */5 `tick` cron and must not run again once the first
    tick past their time has fired.
    """
    date_key = now_local().strftime("%Y-%m-%d")
    rows = _read_ops(_sheet_id())
    return {
        row["component"]
        for row in rows
        if row.get("date") == date_key and row.get("component") in components
    }