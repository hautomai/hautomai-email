from __future__ import annotations

import argparse
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from apify_client import ApifyClient

from apify_scraper import actor_info, fetch_run_dataset, run_scrape
from classifier import classify
from config import ROOT, config, env, save_config
from email_generator import icebreaker, load_template, render
from email_verify import clear_cache, verify as verify_email, verify_batch
from gmail_client import GmailClient, GoogleAuth
import ops
from scheduler import TS_FORMAT, currency_for, local_now, now_utc, plan
from sheets_crm import SheetsCrm
from website_scout import fetch_site


def sheet_id() -> str:
    value = env("SPREADSHEET_ID") or config["sheets"].get("spreadsheet_id") or ""
    if not value:
        raise SystemExit("Spreadsheet not set. Run: python main.py setup-sheet")
    return value


def first_name(row: dict) -> str:
    name = row.get("contact_name") or row.get("agency_name") or "team"
    return name.split()[0]


def max_last_action_ms(row: dict) -> int:
    latest = 0
    for key in ("first_sent_at", "followup_1_at"):
        try:
            ts = datetime.strptime(row[key], TS_FORMAT)
        except (KeyError, ValueError):
            continue
        latest = max(latest, int(ts.timestamp() * 1000))
    return latest


def thread_context(client: GmailClient, thread_id: str) -> tuple[str | None, str | None, str | None]:
    messages = client.get_thread(thread_id)
    references = []
    in_reply_to = None
    subject = None
    for msg in sorted(messages, key=lambda m: int(m.get("internalDate", 0))):
        headers = GmailClient.headers(msg)
        mid = headers.get("message-id")
        if mid and mid not in references:
            references.append(mid)
        in_reply_to = headers.get("message-id") or in_reply_to
        if headers.get("subject"):
            subject = headers["subject"]
    return in_reply_to, " ".join(references[-10:]), subject


def cmd_auth(args):
    creds = GoogleAuth.run_oauth(force=args.force)
    client = GmailClient(creds)
    print("Auth OK. Sender account:", client.my_email())


def money10k(row: dict) -> str:
    return f"{currency_for(row.get('country'))}10k/mo"


def outreach_body(row: dict, fname: str) -> str:
    _, body = load_template("outreach")
    body = render(
        body,
        firstName=fname,
        money10k=money10k(row),
        accountSignature=config["outbound"].get("sender_name", "Hichem"),
    )
    return body


def cmd_setup_sheet(args):
    spreadsheet_id = config["sheets"].get("spreadsheet_id")
    if not spreadsheet_id:
        spreadsheet_id = SheetsCrm.create_spreadsheet(config["sheets"]["spreadsheet_title"])
        config["sheets"]["spreadsheet_id"] = spreadsheet_id
        save_config(config)
    crm = SheetsCrm(spreadsheet_id)
    crm.ensure_sheet()
    print(f"Spreadsheet ready: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")


def cmd_seed(args):
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    test = config["test"]
    to_email = getattr(args, "to", None) or test.get("to_email") or ""
    if not to_email:
        raise SystemExit("No test recipient. Pass --to <email> or set test.to_email in config.yaml.")
    three_days_ago = (now_utc() - timedelta(days=3)).strftime(TS_FORMAT)
    inserted = 0
    for sample in test["sample_rows"]:
        record = {
            "agency_name": sample["agency_name"],
            "contact_name": sample.get("contact_name") or sample["agency_name"],
            "email": to_email,
            "website": sample["website"],
            "status": "new",
            "notes": "seeded sample",
        }
        if sample.get("status") == "outreach_sent":
            record["status"] = "outreach_sent"
            record["first_sent_at"] = three_days_ago
        crm.add_rows([record])
        inserted += 1
    print(f"Seeded {inserted} rows targeting {to_email}")


def cmd_plan(args):
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    queue = plan(crm.rows())
    if not queue:
        print("Nothing due today (or outside send windows).")
        return
    outbound = config["outbound"]
    for item in queue:
        row = item["row"]
        country = row.get("country") or "-"
        local = local_now(row.get("country")).strftime("%H:%M")
        print(f"[{item['kind']:8}] {row['agency_name']:<24} {row['email']}  {country} ({local} local now)")
    print(f"\nDaily cap {outbound['daily_send_cap']} | queued {len(queue)}")


def ingest_items(crm: SheetsCrm, items: list[dict], limit: int) -> int:
    crm.ensure_sheet()
    fresh = [item for item in items if item.get("email")]
    existing = {row["email"] for row in crm.rows() if row.get("email")}
    to_add = []
    skipped_invalid = 0
    new_items = [item for item in fresh if item["email"] not in existing]
    to_verify = [item["email"] for item in new_items if item["email"] not in existing]
    verdicts = verify_batch(to_verify, **_verifier_options()) if to_verify else {}
    for item in new_items:
        result = verdicts.get(
            item["email"], {"verdict": "unknown", "reason": "verification_unavailable", "quality": ""}
        )
        if result["verdict"] == "invalid":
            skipped_invalid += 1
            print(f"  SKIP invalid {item['email']} ({result['reason']})")
            continue
        if result["verdict"] == "unknown":
            print(f"  ? unverifiable {item['email']} (kept, DNS hiccup)")
        if result["verdict"] == "valid":
            item["verified_at"] = now_utc().strftime(TS_FORMAT)
            item["verified_version"] = _verifier_version()
        to_add.append(item)
        existing.add(item["email"])
    crm.add_rows(to_add)
    for item in to_add[:limit]:
        print(f"  {item['agency_name']:<30} {item['email']}")
    print(f"Fetched {len(items)} items, inserted {len(to_add)} new rows, skipped {skipped_invalid} invalid.")
    return len(to_add)


def _apify_tokens() -> list[str]:
    tokens = []
    for key in ("APIFY_TOKEN", "APIFY_TOKEN_BACKUP"):
        tok = env(key)
        if tok and tok not in tokens:
            tokens.append(tok)
    return tokens


def cmd_scrape(args):
    tokens = _apify_tokens()
    if not tokens:
        raise SystemExit("APIFY_TOKEN missing in .env or environment")
    last_error = None
    for token in tokens:
        try:
            return _scrape_with_token(token, args)
        except Exception as exc:
            last_error = exc
            print(f"[apify] token {token[:12]}... failed: {type(exc).__name__}: {exc}")
    raise SystemExit(f"All APIFY tokens failed: {last_error}")


def _scrape_with_token(token: str, args):
    client = ApifyClient(token)
    for info in actor_info(client):
        print(f"Actor: {info['title']} ({info['id']})\n{info['description']}\n")
    if args.info:
        return 0
    if args.dry_run:
        print("Dry-run: no actor run triggered.")
        return 0
    run_id = getattr(args, "run_id", None)
    items = fetch_run_dataset(client, run_id) if run_id else run_scrape(client)
    return ingest_items(SheetsCrm(sheet_id()), items, args.limit)


def cmd_outreach(args):
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    force = bool(getattr(args, "force", False))
    queue = plan(crm.rows(), ignore_window=force)
    counts = {"outreach": 0, "followup": 0}
    if getattr(args, "max_sends", 0):
        queue = queue[: args.max_sends]
    if not queue:
        print("Nothing due today.")
        return counts
    send = bool(getattr(args, "send", False))
    outbound = config["outbound"]
    outreach_subject = load_template("outreach")[0]
    client = GmailClient() if send else None
    print(f"Queue: {len(queue)} email(s) - " + ("SENDING" if send else "PREVIEW ONLY"))
    for index, item in enumerate(queue):
        row = item["row"]
        fname = first_name(row)
        to = getattr(args, "to", None) or row.get("email") or ""
        if not to:
            print(f"[{item['kind']}] {row['agency_name']}: no recipient, skipped")
            continue
        if send and not getattr(args, "to", None) and _needs_reverify(row):
            result = verify_email(to, **_verifier_options())
            if result["verdict"] == "invalid":
                mark = config["email_verification"].get("mark_status", "invalid")
                crm.update_row(
                    row["_row"],
                    {"status": mark, "notes": f"bounce-protect: {result['reason']}"},
                )
                print(f"[{item['kind']}] SKIP invalid recipient -> {to} ({row['agency_name']}): {result['reason']}")
                continue
            if result["verdict"] == "valid":
                crm.update_row(
                    row["_row"],
                    {
                        "verified_at": now_utc().strftime(TS_FORMAT),
                        "verified_version": _verifier_version(),
                    },
                )
        if item["kind"] == "outreach":
            updates = {}
            subject, body = load_template("outreach")
            subject = render(subject, firstName=fname, money10k=money10k(row))
            body = outreach_body(row, fname)
            if not send:
                print(f"\n=== OUTREACH -> {to} [{row['agency_name']}]\nSubject: {subject}\n\n{body}")
                if updates:
                    crm.update_row(row["_row"], updates)
                continue
            _, thread = client.send_email(to, subject, body)
            updates.update(
                {
                    "status": "outreach_sent",
                    "thread_id": thread,
                    "first_sent_at": now_utc().strftime(TS_FORMAT),
                }
            )
            crm.update_row(row["_row"], updates)
            counts["outreach"] += 1
            print(f"[outreach] sent -> {to} ({row['agency_name']}) thread={thread}")
        else:
            subject, body = load_template("followup")
            body = render(
                body,
                firstName=fname,
                money10k=money10k(row),
                conversion=_followup_conversion(row),
            )
            if not send:
                print(f"\n=== FOLLOW-UP -> {to} [{row['agency_name']}]\nSubject: (thread reply)\n\n{body}")
                continue
            thread = row.get("thread_id")
            if thread:
                in_reply_to, references, thread_subject = thread_context(client, thread)
                _, sent_thread = client.send_email(
                    to,
                    thread_subject or f"Re: {render(outreach_subject, firstName=fname, money10k=money10k(row))}",
                    body,
                    thread_id=thread,
                    in_reply_to=in_reply_to,
                    references=references,
                )
            else:
                _, sent_thread = client.send_email(
                    to, f"Re: {render(outreach_subject, firstName=fname, money10k=money10k(row))}", body
                )
            crm.update_row(
                row["_row"],
                {"status": "followup_sent", "followup_1_at": now_utc().strftime(TS_FORMAT)},
            )
            counts["followup"] += 1
            print(f"[followup] sent -> {to} ({row['agency_name']}) thread={sent_thread}")
        if send and index < len(queue) - 1:
            if getattr(args, "fast", False):
                wait = outbound["run_spacing_seconds"]
                print(f"...waiting {wait}s before next send")
            else:
                wait = random.uniform(
                    outbound.get("send_jitter_min", outbound["send_spacing_seconds"]),
                    outbound.get("send_jitter_max", outbound["send_spacing_seconds"]),
                )
                print(f"...waiting {wait:.0f}s before next send")
            time.sleep(wait)
    print("Done." if send else "Preview only - nothing sent.")
    return counts


_BOUNCE_SENDERS = ("mailer-daemon", "mail delivery subsystem", "postmaster@")

_BOUNCE_SUBJECT_MARKERS = (
    "undeliverable",
    "delivery failure",
    "delivery report",
    "couldn't be delivered",
    "returned to sender",
    "failure notice",
    "(failure)",
)

_DELAY_MARKERS = ("delay", "temporar", "will retry", "retry for")


def _is_bounce_message(headers: dict) -> bool:
    sender = headers.get("from", "").lower()
    subject = headers.get("subject", "").lower()
    if any(marker in subject for marker in _DELAY_MARKERS):
        return False
    if any(marker in sender for marker in _BOUNCE_SENDERS):
        return True
    return any(marker in subject for marker in _BOUNCE_SUBJECT_MARKERS)


def _scan_bounces(client: GmailClient, hours: int = 48) -> list[dict]:
    days = max(1, int(hours // 24))
    results = []
    for msg in client.list_messages(f"from:mailer-daemon OR from:postmaster in:anywhere newer_than:{days}d"):
        full = client.get_message(msg["id"], metadata_headers=["From", "X-Failed-Recipients", "To", "Subject"])
        headers = GmailClient.headers(full)
        if not _is_bounce_message(headers):
            continue
        raw = headers.get("x-failed-recipients") or ""
        recipients = [r.strip().lower() for r in re.split(r"[,\s]+", raw) if "@" in r]
        if recipients:
            results.append({"email": recipients[0]})
            continue
        match = re.search(
            r"to\s*[<]?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
            full.get("snippet", ""),
        )
        if match:
            results.append({"email": match.group(1).lower()})
    return results


def _verifier_options() -> dict:
    verification = config["email_verification"]
    return {
        "timeout": verification.get("mx_timeout_seconds", 3),
        "method": verification.get("method", "apify"),
        "require_site": bool(verification.get("require_site", True)),
        "site_timeout": verification.get("site_timeout_seconds", 5),
        "actor_id": verification.get("verifier_actor_id", "account56/email-verifier"),
        "batch_cap": int(verification.get("batch_cap", 100)),
        "fallback": verification.get("fallback_method") == "hunter",
    }


def _verifier_version() -> str:
    return str(config["email_verification"].get("verifier_version", 2))


def _verified_fresh(row: dict, days: int) -> bool:
    verified = row.get("verified_at") or ""
    if not verified:
        return False
    try:
        last = datetime.strptime(verified, TS_FORMAT)
    except ValueError:
        return False
    return (now_utc().replace(tzinfo=None) - last).days < days


def _needs_reverify(row: dict) -> bool:
    verification = config["email_verification"]
    if verification.get("use_sheet_verification", False) and (row.get("verification") or "").strip():
        return False
    if not _verified_fresh(row, int(verification.get("resend_verify_after_days", 7))):
        return True
    return str((row.get("verified_version") or "").strip()) != _verifier_version()


def cmd_poll(args):
    client = GmailClient()
    my_email = client.my_email()
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    bounced_emails = {
        b["email"]
        for b in _scan_bounces(client, int(config["email_verification"].get("bounce_scan_hours", 48)))
    }
    bounced = 0
    for row in crm.rows():
        email = (row.get("email") or "").lower()
        status = row.get("status")
        if not email or email not in bounced_emails:
            continue
        if status in ("outreach_sent", "followup_sent") or (
            status in ("replied", "closed") and (row.get("reply_class") or "").lower() != "positive"
        ):
            crm.update_row(
                row["_row"],
                {"status": "bounced", "notes": "bounce detected by Gmail delivery failure"},
            )
            bounced += 1
            print(f"[bounce] {row['agency_name']:<24} -> {email} (marked bounced)")
    candidates = [
        row
        for row in crm.rows()
        if row.get("thread_id")
        and not row.get("reply_at")
        and row.get("status") in ("outreach_sent", "followup_sent")
    ]
    if not candidates:
        print("No active threads to poll.")
        return 0, bounced
    should_reply = not getattr(args, "no_reply", False) and not getattr(args, "dry_run", False)
    processed = 0
    for row in candidates:
        messages = client.get_thread(row["thread_id"])
        threshold = max_last_action_ms(row)
        inbound = None
        saw_bounce = False
        for msg in sorted(messages, key=lambda m: int(m.get("internalDate", 0))):
            if int(msg.get("internalDate", 0)) <= threshold:
                continue
            sender = GmailClient.headers(msg).get("from", "")
            if my_email.lower() in sender.lower():
                continue
            if _is_bounce_message(GmailClient.headers(msg)):
                saw_bounce = True
                continue
            inbound = msg
        if inbound is None:
            if saw_bounce:
                crm.update_row(
                    row["_row"],
                    {"status": "bounced", "notes": "bounce detected by Gmail delivery failure"},
                )
                bounced += 1
                print(f"[bounce] {row['agency_name']:<24} -> {row.get('email')} (answer was a delivery failure)")
            continue
        verdict = classify(GmailClient.body_text(inbound))
        if verdict == "positive" and should_reply:
            _, body = load_template("reply_positive")
            body = render(body, firstName=first_name(row))
            to = getattr(args, "to", None) or row.get("email") or ""
            in_reply_to, references, thread_subject = thread_context(client, row["thread_id"])
            client.send_email(
                to,
                thread_subject or outbound_subject(row),
                body,
                thread_id=row["thread_id"],
                in_reply_to=in_reply_to,
                references=references,
            )
            status = "positive"
            action = "links reply sent"
        else:
            status = "closed" if verdict == "negative" else "replied"
            action = "no auto-send"
        crm.update_row(
            row["_row"],
            {
                "reply_at": now_utc().strftime(TS_FORMAT),
                "reply_class": verdict,
                "status": status,
            },
        )
        print(f"[poll] {row['agency_name']:<24} -> {verdict:8} ({action})")
    return processed, bounced


def cmd_verify(args):
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    if getattr(args, "recheck", False):
        clear_cache()
    verification = config["email_verification"]
    mark = verification.get("mark_status", "invalid")
    verifier_options = _verifier_options()
    # "only future": by default only gate NEW leads (ungated); a full re-scan
    # of already-outreached rows requires --recheck.
    active = {"new"}
    if getattr(args, "recheck", False):
        active = {"new", "outreach_sent", "followup_sent", "bounced"}
    rows_by_email: dict[str, dict] = {}
    checked = marked = unknown = valid = 0
    for row in crm.rows():
        email = (row.get("email") or "").strip().lower()
        if not email or row.get("status") not in active:
            continue
        if verification.get("use_sheet_verification", False) and (row.get("verification") or "").strip():
            continue
        rows_by_email.setdefault(email, row)
    verdicts = verify_batch(list(rows_by_email), **verifier_options) if rows_by_email else {}
    for email, row in rows_by_email.items():
        result = verdicts.get(
            email, {"verdict": "unknown", "reason": "verification_unavailable", "quality": ""}
        )
        checked += 1
        if result["verdict"] == "invalid":
            if not getattr(args, "preview", False):
                crm.update_row(
                    row["_row"],
                    {
                        "status": mark,
                        "notes": f"invalid: {result['reason']}",
                        "verified_at": now_utc().strftime(TS_FORMAT),
                    },
                )
                print(f"  INVALID {email:<36} ({result['reason']}) row {row['_row']} -> marked")
            else:
                print(f"  INVALID {email:<36} ({result['reason']}) row {row['_row']} (preview)")
            marked += 1
        else:
            valid += 1
            if not getattr(args, "preview", False) and result["verdict"] == "valid":
                crm.update_row(
                    row["_row"],
                    {
                        "verified_at": now_utc().strftime(TS_FORMAT),
                        "verified_version": _verifier_version(),
                    },
                )
            if result["verdict"] == "unknown":
                unknown += 1
                print(f"  ? unverifiable {email} ({result['reason']})")
    print(f"Checked {checked} rows: {valid} ok, {marked} invalid, {unknown} unverifiable.")
    return marked


def outbound_subject(row: dict) -> str:
    return f"Re: {render(load_template('outreach')[0], firstName=first_name(row), money10k=money10k(row))}"


_CONVERSION_SWITCH_DATE = datetime(2026, 8, 24)


def _followup_conversion(row: dict) -> str:
    """Match the follow-up claim to the copy the lead originally received.

    The outreach template switched from '2.5x' to '2x' on 2026-08-24. Rows sent
    before the switch got the 2.5x copy, so their follow-up keeps 2.5x too;
    rows sent after use 2x.
    """
    sent = row.get("first_sent_at") or ""
    try:
        sent_dt = datetime.strptime(sent, TS_FORMAT)
    except (KeyError, ValueError):
        return "2x"
    return "2.5x" if sent_dt.date() < _CONVERSION_SWITCH_DATE.date() else "2x"


def cmd_status(args):
    crm = SheetsCrm(sheet_id())
    crm.ensure_sheet()
    rows = crm.rows()
    for row in rows:
        print(
            f"{row['_row']:>3} | {row['status']:<13} | {row['agency_name']:<22} | "
            f"{row['email']:<30} | {str(row['thread_id'])[:12]:<12} | "
            f"{row['first_sent_at'] or '-':<19} | {row['followup_1_at'] or '-':<19} | "
            f"{row['reply_at'] or '-':<19} ({row['reply_class'] or '-'})"
        )
    summary = crm.summary()
    print("\nCounts:", summary if summary else "empty")


def cmd_test(args):
    client = GmailClient()
    print("Sender:", client.my_email())
    msg_id, thread = client.send_email(args.to, args.subject, args.body)
    print("Sent:", msg_id, thread)


def cmd_icebreakers(args):
    sites = []
    for sample in config["test"]["sample_rows"]:
        website = sample.get("website") or ""
        if "nonexistent" in website or "invalid" in website:
            continue
        sites.append(sample)
    if not sites:
        raise SystemExit("No sample sites in config.yaml > test > sample_rows")
    cache = {}
    for sample in sites:
        cache[sample["website"]] = fetch_site(
            sample["website"], timeout=config["website_scout"].get("timeout_seconds", 12)
        )
    lines = []
    for index in range(args.count):
        sample = sites[index % len(sites)]
        phrase, client = icebreaker(
            cache[sample["website"]],
            agency_hint=sample["agency_name"],
            model=config["llm"].get("icebreaker_model"),
        )
        paragraph = (
            f"Been checking out your portfolio, crazy results for "
            f"{client or 'your recent wins'}. Congrats!"
        )
        lines.append(f"[{index + 1}] {sample['agency_name']} -> {paragraph}")
    output = "\n\n".join(lines)
    print(output)
    out_path = args.out or ROOT / "data" / "icebreakers.md"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output + "\n", encoding="utf-8")
    print(f"\nSaved {len(lines)} icebreakers -> {out_path}")


def run_scrape_ingest(limit: int = 50):
    inserted = cmd_scrape(argparse.Namespace(info=False, dry_run=False, limit=limit))
    ops.heartbeat("scrape", "ok", f"run ok, inserted {inserted} new lead(s)")
    return inserted


def run_outreach():
    counts = cmd_outreach(
        argparse.Namespace(
            send=True,
            to=None,
            max_sends=config["outbound"]["max_sends_per_run"],
            fast=False,
            force=False,
        )
    )
    detail = f"sent {sum(counts.values())} ({counts['outreach']} outreach, {counts['followup']} follow-up)"
    ops.heartbeat("outreach", "ok", detail)
    return counts


def run_tick():
    run_poll()
    return run_outreach()


def run_poll():
    processed, bounced = cmd_poll(argparse.Namespace(dry_run=False, to=None, no_reply=False))
    ops.heartbeat("poll", "ok", f"checked threads, {processed} reply(ies), {bounced} bounce(s)")
    return processed


def run_daily_check(send: bool = True):
    report = ops.run_daily_check(send=send)
    print(report["body"])
    return report


def cmd_monitor(args):
    if getattr(args, "send", False):
        report = run_daily_check(send=True)
    else:
        report = ops.build_report()
        print(report["body"])
        print("\nPreview only (no writes, no email). Use --send to email the digest.")
    return report


def main():
    parser = argparse.ArgumentParser(description="Cold-outreach pipeline (Gmail + Sheets)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="one-time Gmail/Sheets OAuth")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("setup-sheet", help="create the CRM spreadsheet")
    p.set_defaults(func=cmd_setup_sheet)

    p = sub.add_parser("seed", help="insert test rows (no Apify)")
    p.add_argument("--to", default=None)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("plan", help="show today's send queue")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("scrape", help="run the Apify actor, or fetch and ingest an existing run's dataset")
    p.add_argument("--info", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--run-id", default=None, help="fetch dataset of an already-finished run (no new actor run)")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("outreach", help="preview or send outreach + follow-ups")
    p.add_argument("--send", action="store_true")
    p.add_argument("--to", default=None)
    p.add_argument("--max-sends", type=int, default=0)
    p.add_argument("--fast", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_outreach)

    p = sub.add_parser("icebreakers", help="generate N sample icebreakers (no sending)")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--out", default=None, help="output file (default data/icebreakers.md)")
    p.set_defaults(func=cmd_icebreakers)

    p = sub.add_parser("poll", help="check replies, update CRM, auto-reply on positive")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-reply", action="store_true")
    p.add_argument("--to", default=None)
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("verify", help="DNS-check pending emails, mark invalid rows")
    p.add_argument("--preview", action="store_true", help="show results without writing to the CRM")
    p.add_argument("--recheck", action="store_true", help="clear the per-domain DNS cache first")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("status", help="show CRM table")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("monitor", help="preview or send the daily pipeline health digest")
    p.add_argument("--send", action="store_true")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("test", help="send a raw test email")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()