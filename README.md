# PPC Outreach Pipeline (Python + Modal)

Cold-outreach automation: Apify scrape -> Google Sheets CRM -> LLM icebreaker (visits the
agency website) -> Gmail outreach -> day-3 follow-up (threaded) -> reply detection ->
keyword classification -> template auto-reply with Loom + Google Drive links.

Runs locally for dev, and 100% on [Modal](https://modal.com) for production.

## Setup (one-time)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

1. **Secrets** - already in `.env`:
   - `apify: <token>` (APIFY_TOKEN)
   - `openrouter: <key>` (OPENROUTER_API_KEY) and `openrouter model` (OPENROUTER_MODEL)
2. **Google Cloud**: enable Gmail + Sheets + Drive APIs, create OAuth Desktop client, put
   `credentials.json` in this folder (done for tests).
3. `python main.py auth` - one browser consent, generates `token.json`.
4. `python main.py setup-sheet` - creates the CRM spreadsheet, ID saved to `config.yaml`.

## Local CLI

| Command | Purpose |
|---|---|
| `python main.py seed [--to EMAIL]` | Insert test rows from `config.yaml > test` (no Apify) |
| `python main.py plan` | Show today's queue (follow-ups first, then outreach, cap 12/day) |
| `python main.py scrape [--info] [--dry-run]` | Run Apify actor + ingest into CRM (invalid emails dropped) |
| `python main.py outreach [--send] [--to EMAIL] [--fast] [--max-sends N]` | Preview (default) or send |
| `python main.py poll [--dry-run] [--no-reply]` | Detect replies, stop follow-ups, classify, auto-reply on positive |
| `python main.py verify [--preview] [--recheck]` | DNS-check pending emails, mark invalid rows (`invalid` status) |
| `python main.py icebreakers [--count N]` | Sample N icebreakers from test sites (no sending) |
| `python main.py status` | Show the CRM table |
| `python main.py test --to X --subject S --body B` | Raw test email |

Safety: nothing cold-sends unless `--send`. `--to` overrides every recipient during tests.
Cap 12 emails/day, staggered; send window 09:00-18:00 Europe/London (use `--force` to bypass).
Emails are verified before insert and before every send: Verifalia API when enabled
(`method: verifalia`, free 25 checks/day), falling back to a local DNS MX/A check; invalid
recipients get `status: invalid` + reason in `notes` instead of a bounce. Gmail delivery
failures detected by `poll` are marked `status: bounced`.

## Config (config.yaml)

- `outbound`: cap, cadence (`followup_days: [3]`), window, spacing
- `reply`: positive/negative keywords
- `links`: Loom + Google Drive URLs for the positive reply
- `llm`: OpenRouter model
- `email_verification`: method (`dns` or `verifalia`, auto-fallback to DNS), `mark_status`, `mx_timeout_seconds`, `smtp_timeout_seconds`, `bounce_scan_hours`
- `test`: seed rows + test recipient
- `sheets.spreadsheet_id`: set by `setup-sheet`

Provider credentials: `VERIFALIA_SID` + `VERIFALIA_XAUTH_TOKEN` (from https://verifalia.com,
free 25 verifications/day). Fallback to local DNS is automatic when unset.

## Deploy to Modal

```bash
pip install modal && modal token new
modal secret create ppc-outreach-secrets \
  APIFY_TOKEN=... OPENROUTER_API_KEY=... OPENROUTER_MODEL=deepseek/deepseek-v4-flash \
  SPREADSHEET_ID=... GMAIL_TOKEN_JSON='<contents of token.json>' \
  VERIFALIA_SID=... VERIFALIA_XAUTH_TOKEN=...
modal deploy email_app.py
```

One scheduled function (`tick`, every 5 min) runs the whole pipeline: poll +
outreach/follow-ups on every tick, plus the daily jobs dispatched from it — the
scrape at 00:30 UTC and the 19:00 UTC health digest. Local machine no longer required.

Modal's workspace plan allows five scheduled functions in total and this app used
to hold three of them, which blocked a sibling project from deploying. The two
daily jobs now ride the `*/5` tick: each fires on the first tick at or after its
time, guarded by the Ops-tab heartbeat (`ops.ran_today`) so it can never double-run.
Times are unchanged.