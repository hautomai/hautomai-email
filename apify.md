actor: https://console.apify.com/actors/eY5rI8N4dDhAVyxlq/input

{
    "companyIndustryIncludes": [
        "Marketing & Advertising"
    ],
    "companyKeywordIncludes": [
        "PPC",
        "Paid ads",
        "Paid media",
        "Paid advertising",
        "Ads",
        "Google Ads",
        "Meta Ads",
        "Facebook Ads",
        "Linkedin Ads"
    ],
    "companyKeywordMode": "phrase",
    "companyLocationCountryIncludes": [
        "United Arab Emirates"
    ],
    "companySizeIncludes": [
        "1-10",
        "11-50"
    ],
    "countOnly": false,
    "dontSaveProgress": false,
    "hasPhone": false,
    "includeTitleVariants": false,
    "personLocationCountryIncludes": [
        "United Arab Emirates"
    ],
    "personTitleIncludes": [
        "CEO",
        "Owner",
        "Founder",
        "Co-Owner",
        "Co-Founder",
        "Director"
    ],
    "resetProgress": false,
    "seniorityIncludes": [
        "c_suite",
        "director",
        "owner",
        "partner"
    ],
    "totalResults": 50
}

Note: emailStatusIncludes is intentionally omitted so verified AND unverified
emails are returned; the pipeline re-verifies every lead itself before any send.

---

## Quality gate (before outreach)

Each scrape cycle runs a quality check **before** leads are added to the CRM:

1. Scrape 50 leads with the actor's current filters (verified + unverified returned).
2. Fetch each lead's website (homepage only, in parallel) and scan the text for
   PPC / paid-media signals (`ppc`, `pay per click`, `google ads`, `paid search`,
   `paid media`, `paid advertising`, `bing/fb/meta ads`, `media buying`,
   `performance marketing`, ...).
3. A lead is **relevant** only if its site loads AND contains a PPC signal.
   Unreachable / no-website / no-signal leads are dropped before email verification.
4. Only the **relevant** leads pass through email verification and are ingested.
   No re-scrape attempts, no pass-rate threshold - a single scrape per night where
   the gate simply keeps the relevant subset.

Config lives in `config.yaml` > `apify.quality`. `ppc_terms` sets the signal
vocabulary; `enabled` toggles the gate; `site_timeout_seconds` / `max_workers`
control homepage fetching. The gate is skipped when `--run-id` is used and when
`scrape --dry-run` is used.
