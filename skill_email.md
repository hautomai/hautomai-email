---
name: email-pipeline-ops
description: Operate, debug, and modify the PPC cold-email outreach pipeline in /path/to/email. Use this whenever the user asks to run, check, fix, change copy, reconfigure, or redeploy the email pipeline (scrape, outreach, follow-ups, poll, verify, monitor, Modal deploy, OAuth re-auth, Apify, Gmail, CRM). Always use this skill for any task touching this pipeline.
---

# PPC Outreach Email Pipeline — Operations Skill

Cold-outreach automation: Apify scrape → quality gate → Google Sheets CRM → email verification → Gmail outreach → day-3 threaded follow-up → reply poll/classify → auto-reply with Loom + Drive links → daily health digest. Runs locally for dev, 100% on Modal in production. No LLM/AI anywhere (personalization disabled).

## File map

| File | Role |
|---|---|
| `main.py` | CLI (auth, setup-sheet, seed, plan, scrape, outreach, poll, verify, status, icebreakers, monitor, test) + Modal entrypoints (`run_scrape_ingest`, `run_tick`, `run_poll`, `run_outreach`, `run_daily_check`) |
| `email_app.py` | Modal deploy file: one `*/5min` `tick` cron (poll + outreach) that also dispatches the daily scrape (00:30 UTC) and 19:00 UTC health digest, secret `ppc-outreach-secrets`, snapshot rules |
| `config.py` | `.env` loader (key normalization + `_ALIASES`), `config.yaml` load/save |
| `config.yaml` | Apify actor + filters, quality gate, countries (tz/currency), LLM, outbound caps/windows, reply keywords, links, verification, sheets, monitoring, test rows |
| `apify_scraper.py` | Actor run, dataset fetch, normalize, quality gate wiring |
| `lead_quality.py` | PPC-signal scan of scraped homepages (parallel), keeps confirmed only |
| `website_scout.py` | `fetch_homepage` (fast, gate) and `fetch_site` (case-study walk, legacy icebreaker input) |
| `email_verify.py` | Verification chain: syntax → site-reachable → Apify Million Verifier actor (batch) |
| `email_generator.py` | `render()` `{{placeholders}}`, `load_template()`; legacy OpenRouter LLM/icebreaker INERT (`llm.enabled: false` in config) |
| `gmail_client.py` | OAuth (GoogleAuth), Gmail send/thread/message, `_retry` (429/5xx + 400 failedPrecondition), rate limiter |
| `scheduler.py` | Timezone logic, send windows, `plan()` (daily cap, follow-up due, queue order) |
| `classifier.py` | Keyword reply classification (positive/negative/neutral) |
| `sheets_crm.py` | CRM (Google Sheet) read/write, columns, summary |
| `ops.py` | Ops tab heartbeats, alerts, daily health report |
| `templates/outreach.md` | Outreach email (Subject line = first line: `Subject: ...`), placeholders `{{firstName}} {{money10k}} {{accountSignature}}` |
| `templates/followup.md` | Day-3 follow-up, placeholder `{{conversion}}` (2.5x or 2x) |
| `templates/reply_positive.md` | Auto-reply on positive: Loom + Drive links |
| `email.md`, `follow-up.md`, `reply.md`, `links.md` | User's source copy — keep templates in sync with these when copy changes |
| `temp-follow-up/follow_up.py` | Standalone follow-up sender for 16 legacy manual leads (self-contained, cron-managed, state in `state.json`) |

## The copy (message funnel)

The pipeline sends a 3-message sequence. Sources of truth are `email.md`, `follow-up.md`, `reply.md`, `links.md`; the rendered versions live in `templates/*.md` and must be kept in sync.

### 1. Outreach (cold email) — `templates/outreach.md`
```
Subject: {{firstName}}?


Hey {{firstName}}. you will make an additional {{money10k}} in revenue by fixing
this revenue leak: a hot lead clicks, submits the contact form, doesn't hear
back for 1h and goes cold.

I fixed it for Symplify Marketing to capture and follow-up with all their leads
in under 1min. An agency like yours will 2x their clients' conversion rate with
that.

I've actually gone ahead and built it for you.
Wanted to offer it free. I have to show a bit of value before people are willing
to work with me.

Reply 'yes' and I'll send it over with a 3-min Loom walking through it.

Thx,
{{accountSignature}}
```
Message architecture: **first-name subject line** → **pain** (1h follow-up gap kills hot leads) → **proof** (Symplify Marketing, <1min, 2x clients' conversion) → **zero-risk offer** (built already, free) → **micro-CTA** ("Reply 'yes'"). Tone is casual, human, no corporate speak. Note: `money10k` appears WITHOUT a trailing "/mo" because `main.py > money10k()` already appends it ("$10k/mo").

### 2. Follow-up (day 3) — `templates/followup.md`
TL;DR bullets repeating the offer: `{{conversion}}` conversion-rate claim, capture/score/enrich/reply in 1min, Symplify guarantee, "it's free", 3-min Loom link, signoff Hichem. Sent as a threaded `Re:` reply (In-Reply-To/References set from the original thread).

### 3. Positive auto-reply — `templates/reply_positive.md`
Triggered when the classifier flags a reply as positive. Sends Loom + Drive links ("everything you need to set it up yourself") and the DFY upsell: *"Guaranteed: an additional 10k in 30 days, or your money back."* Only fires automatically when the lead used positive keywords; everything else is parked for manual handling.

### Links & signature
- Loom + Google Drive URLs live in `links.md` and `config.yaml > links`, and are ALSO hardcoded in `templates/followup.md` and `templates/reply_positive.md` — when links change, update all four places.
- `{{accountSignature}}` = `config.yaml > outbound.sender_name` ("Hichem"); the follow-up hardcodes "Hichem" — keep in sync.
- `{{money10k}}` = "$10k/mo" or "£10k/mo" per the lead's country (`config.yaml > countries`) so the pitch lands in their currency.

## Personalization

**DISABLED — no LLM.** `config.yaml > llm.enabled: false`; the pipeline no longer calls OpenRouter. The outreach email is fully static (`{{firstName}}`, `{{money10k}}`, `{{accountSignature}}` only). Legacy pieces still present but inert: `icebreaker()` in `email_generator.py` returns the canned fallback immediately, and `main.py icebreakers` samples the same fallback. Re-enable AI by setting `llm.enabled: true` and restoring the `{{clientName}}` hook in `templates/outreach.md`.

## Critical operational facts (do not forget)

1. **Deploys are build-time snapshots.** `email_app.py` copies only `.py/.md/.yaml` to Modal. Any config/template change requires `modal deploy email_app.py`. `.env` is NEVER deployed — Modal reads env from the `ppc-outreach-secrets` secret; when a new key is added to `.env` it must also be added to the Modal secret.
2. **OAuth 7-day cycle.** Google project is in "Testing" mode: refresh tokens expire after 7 days. Fix: user runs `python main.py auth` locally, then `GMAIL_TOKEN_JSON` in the Modal secret is rebuilt from `token.json`. Never assume the token is fresh — check `my_email()` first.
3. **Apify token failover.** `cmd_scrape` tries `APIFY_TOKEN` then `APIFY_TOKEN_BACKUP` (both in `.env` and Modal secret). Free plan ≈ $5/mo; scrape costs ~$0.10–0.15/night. If a key runs out of credits, add/replace the backup, not the code.
4. **Quality gate.** One scrape per night keeps only leads whose homepage loads AND contains PPC terms (`config.yaml > apify.quality.ppc_terms`). No re-scrape, no pass-rate bar. ~20–30% of scraped leads survive; that's expected.
5. **Follow-up conversion consistency.** Rows sent before 2026-08-24 used "2.5x" copy; follow-ups for them must say 2.5x (`_followup_conversion`). New rows use "2x". Never hardcode one value in `templates/followup.md`.
6. **Verification chain.** Syntax → domain-reachable (`require_site`, free local) → **Apify Million Verifier actor** (`account56/email-verifier`, ~$1/1k, batch). Map: actor `result == "ok"` → valid; `invalid` → invalid (blocks send); catch-all/risky/unknown/error → "unknown" (kept, flagged). Only "invalid" blocks a send. Emails that fail a local gate never reach the actor (no charge). Verifier is **future-only**: only NEW leads are gated; already-outreached rows are not re-verified (full rescan = `verify --recheck`).
7. **Never send without `--send`.** CLI previews by default; `--to` overrides every recipient (test safety). Modal runs always send (that's intended).
8. **CRM states:** `new` → `outreach_sent` → (`followup_sent` | `replied`/`closed`/`positive`) ; `invalid`/`bounced` are dead ends. `reply_class`: positive/negative/neutral.

## CLI reference (run with `.venv/bin/python main.py ...`)

| Command | Purpose |
|---|---|
| `auth [--force]` | OAuth consent → `token.json` (required after 7-day expiry) |
| `setup-sheet` | Create CRM spreadsheet, save ID to config.yaml |
| `seed [--to EMAIL]` | Insert config.yaml test rows (no Apify) |
| `plan` | Show today's queue (follow-ups first, 12/day cap, send window) |
| `scrape [--info] [--dry-run] [--limit N] [--run-id ID]` | Run actor + gate + verify + ingest; `--run-id` skips actor run and gate |
| `outreach [--send] [--to E] [--max-sends N] [--fast] [--force]` | Preview or send outreach + follow-ups |
| `poll [--dry-run] [--no-reply]` | Bounce scan + reply detection + classify + auto-reply |
| `verify [--preview] [--recheck]` | Bulk re-verify active rows, mark invalid |
| `status` | Print CRM table + funnel counts |
| `icebreakers [--count N] [--out FILE]` | Sample icebreakers from test sites |
| `monitor [--send]` | Build or email daily health digest |
| `test --to E --subject S --body B` | Raw test email |

## Config knobs (config.yaml)

- `outbound`: `daily_send_cap: 12`, window 09:00–18:00 `Europe/London` (per-lead country timezone), `followup_days: [3]`, spacing `send_spacing_seconds` 2400 + jitter 900–1800, `max_sends_per_run: 3`.
- `apify`: actor `eY5rI8N4dDhAVyxlq`, `country_include` (6 countries), actor_input filters (Marketing & Advertising, PPC keywords phrase-mode, sizes 1–50, C-suite/founder/director/owner/partner), `totalResults: 50`, `quality` block (`enabled`, `ppc_terms`, `site_timeout_seconds`, `max_workers`).
- `reply`: positive/negative keyword lists (classifier is word-boundary regex, plain text).
- `llm`: `enabled: false` (AI/OpenRouter DISABLED). Legacy `icebreaker_model: anthropic/claude-sonnet-5`, `model: deepseek/deepseek-v4-flash` — inert until re-enabled. Set `enabled: true` to reactivate; outreach is fully static meanwhile.
- `email_verification`: `method: apify`, `verifier_actor_id: account56/email-verifier`, `batch_cap: 100` (insurance cap on chargeable emails per batch), `mark_status: invalid`, `bounce_scan_hours: 48`, `resend_verify_after_days: 7`, `verifier_version: 2` (do NOT bump — bump forces global re-verify; keep only-new-leads verification).
- `countries`: timezone + currency per country (drives send windows and `{{money10k}}`).
- `sheets`: `spreadsheet_id`, sheet name `CRM`. `monitoring.recipient`: empty = digest goes to the sender itself.

## Modal (production)

- Deploy: `modal deploy email_app.py`. One scheduled function: `tick` */5 (poll then outreach), which also dispatches the daily jobs when their time comes — `scrape` at 00:30 UTC and `monitor` (health digest) at 19:00 UTC. Times are unchanged; each runs once a day, guarded by the Ops-tab heartbeat (`ops.ran_today`). Three separate crons were merged into this one because the workspace plan allows five scheduled functions in total and this app was spending three of them.
- Secrets live in `ppc-outreach-secrets` (keys: `APIFY_TOKEN`, `APIFY_TOKEN_BACKUP`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `VERIFALIA_SID`, `VERIFALIA_XAUTH_TOKEN`, `HUNTER_API_KEY`, `GMAIL_TOKEN_JSON`, `SPREADSHEET_ID`). Add to it via `modal.secret.Secret.from_name("ppc-outreach-secrets").update({...})` when `.env` gains a key.
- Every scheduled fn is wrapped in `_guarded` → `ops.alert` on exception (writes Ops tab + emails digest recipient). Failures also land in CRM Ops tab (`heartbeat`).
- Check runs: `modal app logs ppc-outreach` or App UI. Health digest (19:00) shows scrape/outreach/poll status, funnel counts, sends today, replies today, stale-poll detection (20 min).

## Incident runbook

1. **Gmail `400 failedPrecondition` on thread fetch** — transient API glitch; `_retry` now retries 400 failedPrecondition with backoff. If persistent, fetch the thread manually (`GmailClient().get_thread(id)`) to confirm it exists; a missing/deleted thread usually means the row's `thread_id` is stale — clear it or mark row closed.
2. **RefreshError / auth failures** — 7-day token expired: user re-runs `main.py auth`, rebuild `GMAIL_TOKEN_JSON` in Modal secret, redeploy or rely on secret update.
3. **Apify no credits** — swap primary/backup order or add new backup key in `.env` AND Modal secret (`APIFY_TOKEN_BACKUP`). Failover prints which token failed.
4. **Scrape returns 0 or very few leads** — check Modal logs for QUALITY line (`confirmed=X/Y`); low confirm rate usually means actor filters too narrow or gate terms too strict. Verify actor input at `apify.md` link; adjust `config.yaml` (actor_input or ppc_terms), redeploy.
5. **Outreach not sending** — `plan` empty = cap reached, outside window, or no rows in `new`/due follow-ups. Use `--force` for window bypass (test only), check `Ops` tab statuses.
6. **Bounces marked** — poll scans `from:mailer-daemon OR from:postmaster` last 48h; bounced rows get `status: bounced` and are excluded from queue automatically.
7. **Actor permission / cost** — `account56/email-verifier` needs one-time permission approval on the Apify account (the token in `.env`/secret, currently `simpatico_playhouse`). If an actor call returns `ForbiddenError requires permission`, approve at console.apify.com/actors/VJ5w50TP6mAbyimyO. Cost ~$1/1k verified; local gates + `batch_cap: 100` bound spend (~cents/day at current scale).

## Change procedure (copy, config, code)

1. Copy changes: edit the source of truth (`email.md`, `follow-up.md`, `reply.md`, `links.md`), then sync into `templates/*.md` (Subject line = first line for outreach) and the hardcoded links/signature. Preserve the message architecture when editing (personal hook → pain → proof → offer → micro-CTA; keep CTAs aligned with `reply.positive_keywords`). Follow-ups must keep `{{conversion}}` parametric.
2. Config changes: edit `config.yaml` directly (no rebuild needed locally; deploy for Modal).
3. Code changes: keep module boundaries above; run `python -m py_compile` on touched files; smoke-test with `--dry-run`/preview (never `--send` for tests, use `--to` override).
4. Deploy: `modal deploy email_app.py` and verify the new build via Modal app logs.
5. If a new env key is added to `.env`, add it to the Modal secret too (snapshot rule #1).

## Temp-follow-up (legacy)

`temp-follow-up/follow_up.py` handles 16 manually-outreached leads that never got a follow-up. Self-contained (no imports from project), reads `../token.json`, writes `state.json`, cron at 09:30 with `--limit 8`, removes its own cron entry when all 16 done. Body hardcoded to 2.5x (all legacy). Only touch it if the user asks about these specific leads.
