# Lead Discovery Agent

An agent that takes a search prompt, an event brochure PDF, or both, and finds sponsors and college delegations. It reads public web pages, identifies organizations, and grounds either a decision-maker or, when no person-specific contact can be verified, a clearly labeled company-level contact. Contact verification and local CSV/SQLite export are implemented; Google Sheets export is optional when local service-account credentials are configured. The final goal is a sheet of verified leads.

The agent never invents data. Gemini is used only for language tasks. URLs, domains, emails and grounding checks are done in code.

## Status

| Part | Description | Status |
|---|---|---|
| 1 | Input processing: prompt and PDF to `SearchPlan` | Done |
| 2 | Lead discovery: search, filter, classify, crawl, re-plan | Implemented. Offline tests pass. Live run verification in progress |
| 3 | Decision-maker identification: role-based extraction, grounding, ranking and snippet fallback | Implemented. Offline tests pass |
| 4 | Contact extraction and verification (code only) | Implemented. Offline tests pass |
| 5 | Validation, deduplication and export to CSV/SQLite with optional Google Sheets | Implemented. Offline tests pass; Sheets requires local credentials |

## Requirements

- Windows 10/11 with PowerShell (the commands below are PowerShell)
- Python 3.12
- A free Google AI Studio API key. **Do not enable billing** on the project.

## Setup

Run these from the project folder.

```powershell
# 1. Create and activate a virtual environment
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install dependencies
python -m pip install -U pip
pip install -r requirements.txt

# 3. Create your settings file
Copy-Item .env.example .env
notepad .env
```

In `.env`, set:

- `GEMINI_API_KEY`: your key from https://aistudio.google.com/app/apikey
- `MODEL_FAST` and `MODEL_SMART`: model names your key accepts
- `OFFLINE=1` while developing, so nothing reaches the API. Set `OFFLINE=0` only when you explicitly intend to run a live search/model workflow.

Check which models work. This sends one small request per model:

```powershell
python scripts\check_models.py
```

Each name should print `WORKS`. Remove any name that prints `FAILS` or `MISSING`.

## Run

Input is a prompt, a PDF, or both. Discovery runs after planning unless you pass `--offline`.

```powershell
# Prompt only
python main.py --prompt "Find tech companies in Bengaluru for a 2000-person college fest"

# PDF brochure only
python main.py --pdf data\sample_brochure.pdf

# Both
python main.py --prompt "Find sponsors for this fest" --pdf data\sample_brochure.pdf

# Offline: no Gemini calls, no web search. Uses templates, regex and cached answers
python main.py --offline --pdf data\sample_brochure.pdf
```

Running with no arguments prints a usage message and exits with code 1.

The program prints the plan (source, category, city, theme, industries, seed organizations and queries), then the discovery results (organizations found, with their homepage and whether they have a usable fetched page).

### Low-token, evidence-focused sample

To search one industry and city while allowing at most one Gemini classification call:

```powershell
python -m scripts.run_small_sample --city "Kolkata" --industry "renewable energy"
```

This discovery-only run uses at most 1 Gemini call. It runs up to 3 broad searches, 2 public social searches, and up to 7 exact-name lookups seeded from public LinkedIn/Instagram snippets or names explicitly present in list-result snippets. Social URLs are never treated as organization websites; list-result snippets only seed a separate company-name lookup. At most 30 non-social results are classified in one batch; output is capped at 3 organizations and 3 pages per organization by default. It writes evidence, verification flags, status codes, and token usage to `outputs/small_sample.json`.

For an end-to-end live test with fewer tokens, add `--full-pipeline`:

```powershell
python -m scripts.run_small_sample --city "Kolkata" --industry "renewable energy" --full-pipeline
```

Deterministic city mode skips the Step 1 model call, limits classification to 18 candidates, batches all selected organizations (3 by default, at most 5) into one Step 3 call, and permits at most one Step 3 fallback call within a 3-call Gemini budget. Transient API failures may cause additional retry attempts; the reported token usage is the provider's response metadata. It runs live search/page fetch and Step 4's ordinary contact checks, then writes Step 5 outputs to `outputs/live_sample/`. Google Sheets is forcibly disabled for this test. Set `OFFLINE=0` and configure `GEMINI_API_KEY` in the local `.env`; the runner exits before searching if these are missing or offline mode remains enabled. Never paste the key into chat.

The corresponding research brief is in `data/sponsor_search_brief.txt`. Company-directory hosts are filtered as discovery sources, and a candidate homepage must also resemble the organization's brand or initials. Public social/list snippets can seed bounded name lookups, but are never treated as company-site evidence. Enrichment is capped (two social searches and up to four exact-name lookups) and only runs when fewer than `MIN_ORGS` candidates have usable official/archived pages. Brochure seeds are resolved first by exact-name search and code-only domain checks. Location evidence, direct sponsorship wording, and weaker CSR/community signals are reported separately; CSR keywords alone do not count as sponsorship.

## How it works

### Part 1: Input processing

1. **Prompt path:** one Gemini call extracts category, city and industries from the request.
2. **PDF path:** PyMuPDF extracts the text, which uses no tokens. One Gemini call reads the event facts. Organization names from Gemini are kept only if they appear in the PDF text, so invented names are dropped. A heading scan also picks up names under headings such as "Past Sponsors".
3. **Queries:** built from code templates, never from Gemini. Event hosts are excluded from the seed organizations.

### Part 2: Lead discovery

1. **Search:** queries run through DuckDuckGo (`ddgs`) with a TTL cache. Duplicate URLs are normalized and removed before Gemini classification.
2. **Resolve seeds first:** names grounded in a brochure are searched directly; a seed is admitted only when a result matches the name and the domain resembles the brand. A seed lookup never relies on Gemini to invent a URL.
3. **Filter and classify:** code removes blocked domains/file types/listicles; Gemini classifies batches of up to 30. Names must be grounded in the original title/snippet. If Gemini is unavailable, tightly constrained domain-matched rows can be retained as `needs_review`, never as verified relevance.
4. **Candidate vs. usable-page counts:** every brand/domain-matched organization can be retained as a review candidate even if its site fails to load. Only organizations with usable official or archived page text count toward `MIN_ORGS` and suppress re-planning. A search snippet is labeled `snippet_only`, not treated as an official page or success.
5. **Crawl:** fetch the homepage, score same-domain links, and fetch a small number concurrently. Requests have bounded retries and a Step 2 discovery time budget. A TTL page cache avoids re-downloading successful pages on immediate reruns.
6. **Failure ladder:** transport errors retry; HTTP 401/403/429 are respected; simple URL variants are tried where appropriate; public Wayback snapshots are labelled archival evidence. A richer archived copy may supplement a thin live page, but never replaces it as current proof.
7. **Re-plan (at most one round by default):** when usable official/archived pages are below `MIN_ORGS`, Gemini may propose up to four new queries. Prompt and validator both require 3–5 words; repeats, URLs and banned terms are rejected.
8. **Stop conditions:** stop on the run time budget, candidate cap, usable-page cap, call budget, or re-plan limit. The CLI reports candidates separately from organizations with usable pages.

### Part 3: Decision-maker identification

Step 3 receives all retained candidates, but only usable official/archived page text can form primary evidence windows. Candidates without role-bearing text remain in `lead_all.csv`; snippet fallback is separately bounded. City proof is not a precondition for looking for a person, and Step 5 remains responsible for the final live-page location gate. Step 3 builds merged windows around configured role keywords, labels each window with its source URL, caps context at 6,000 characters per organization, and batches up to `BATCH_ORGS` organizations per Gemini `smart` request.

Gemini only proposes a name, role, organization ID, and source URL. Code rejects candidates unless the person name is present in the original source text (fuzzy score at least 92), a matching target role is supported within 300 characters, and the returned URL exactly matches a labelled page. Candidates are ranked by role priority, page type, and live-versus-Wayback source; the highest-ranked person is selected and the others are retained for audit. Generic entries and prompt-injected claims are not accepted without source grounding.

If no grounded person is found, Step 3 may search the organization's own domain for the highest-priority role and inspect same-domain result snippets. This fallback is limited by `MAX_SNIPPET_FALLBACKS` (default 2); snippet-only candidates remain clearly marked and never become official-page evidence. Step 3 never fabricates a name or visits LinkedIn to infer employment.

The offline Step 3 tests use fake model and search functions, so they make **zero API calls and zero live searches**:

```powershell
python -m tests.test_step3_people
python -m tests.test_step3_roles
python -m tests.test_step3_cli
python -m tests.test_step4_contacts
```

### Part 4: Contact extraction and verification

Step 4 is code-only; Gemini never generates or verifies a contact. It scans live official-domain HTML/text for `mailto:` and plain emails, de-obfuscates common `[at]`/`[dot]` forms, and decodes Cloudflare `data-cfemail`. Every observed public email is retained in the review data with its source and an explicit unverified status, including generic inboxes. A person-specific email is `Verified` only when it was found on a live official page, matches the Step 3 person, passes syntax validation, and its email domain has MX records. It may use the organization's domain or a recognized public provider such as Gmail or Yahoo, but the address must match the grounded person. If Step 3 finds no person or no person-specific contact passes, an email published on a live official company page may qualify as a company-level route if its syntax passes and its domain has MX records. Company-domain addresses must be shared/role-based; a public-provider address may be shared or personal-looking, but is accepted only because the official page publishes it. Shared inboxes are labeled `General Contact, Company Contact`; a personal-looking public-provider address without a grounded person is labeled `Published Contact, Company Contact`. Neither label claims that Step 3 identified a decision-maker. If no contact passes these checks, the organization remains a review candidate rather than a verified lead. If no contact passes these checks, the organization remains a review candidate rather than a verified lead.

If no exact personal email qualifies, the agent may derive an address only from a dominant same-domain format observed in at least two distinct personal-looking addresses. These are labeled `Pattern-Derived`, never `Verified`. As a final fallback, profile links must be present in the official page HTML, return HTTP 200 on a single ordinary request, and match the person or organization. No login, retries, SMTP probing, or bot-protection bypass is used. Wayback-only contact data is not considered current verification. Each accepted contact retains its evidence/supporting URLs and passed checks in the result and CLI output.

Offline tests inject fake MX and profile responses and make **no DNS, HTTP, API, or live search calls**:

```powershell
python -m tests.test_step4_contacts
```

### Part 5: Validation and export

Step 5 makes **zero Gemini calls**. It validates and deduplicates accepted rows, requires the requested city (including a configured alias) to appear in a live official-domain page, and records row-level drop reasons. It writes `outputs/lead_all.csv`, `outputs/leads.csv`, `outputs/leads.db`, and `outputs/audit_log.csv` before any optional Sheets connection. `lead_all.csv` has one row per discovered organization and retains candidate/page status, location and sponsorship evidence, Step 3 person candidates, public emails observed by Step 4, verified contacts, and review reasons. These review rows are not accepted leads. `leads.csv` and SQLite `leads` remain verified-only. The database also records `candidate_review` history and every run in `pipeline_runs`, including runs with zero accepted leads. Sponsorship evidence is recorded for review but is not currently a hard Step 5 export gate; location and a verified contact are.

Google Sheets is optional and is attempted only when both `SHEET_ID` and `GOOGLE_CREDENTIALS` are set and the credential file exists. Without them, local exports still complete and no Google request is made. For a later Sheets test, enable the Google Sheets and Drive APIs in a no-billing Cloud project, create a service account, save its JSON key locally as `credentials.json`, share the target spreadsheet with that service-account email as Editor, and set the two variables in `.env`. Never paste or commit the JSON key. The agent does not change the sheet's sharing permissions.

The offline export tests use a temporary SQLite database and a fake Sheets client; they make **no Google API or network calls**:

```powershell
python -m tests.test_step5_export
```

## Query rules

- Company runs (`Sponsor`, `Both`) use only the industries and city. Words such as "sponsor", "college", "fest", "delegation" and "student" are never added.
- College runs (`College Delegation`) use college templates.
- Every query is at most 5 words, because longer queries return few or no results.
- At most 10 queries per plan.

## Configuration (`.env`)

```bash
GEMINI_API_KEY=your_key
MODEL_FAST=gemini-3.1-flash-lite,gemini-flash-lite-latest
MODEL_SMART=gemini-3.5-flash,gemini-3-flash-preview
RPM_LIMIT=5
RPD_LIMIT=20
MAX_ORGS=20
BATCH_ORGS=5
MAX_CALLS_PER_RUN=10
MAX_SNIPPET_FALLBACKS=2
MIN_ORGS=5
REPLAN_ROUNDS=1
MAX_CANDIDATES=40
MAX_SEED_ORGS=10
MAX_ENRICHMENT_SEEDS=4
TIME_BUDGET_MIN=8
SEARCH_CACHE_TTL_HOURS=48
PAGE_CACHE_TTL_HOURS=72
OFFLINE=1
SHEET_ID=
GOOGLE_CREDENTIALS=credentials.json
```

Each model name in `MODEL_FAST` and `MODEL_SMART` is tried in order. When one model reaches its daily limit, the next one in the chain is used.

## Quota and caching

- The free tier allows about 20 requests per model per day. Limits reset at midnight Pacific Time.
- Limits apply per Google Cloud project, not per key.
- Gemini answers and non-empty search results are cached. A repeated input reuses cached data; empty or failed searches are not cached permanently, so a later run can retry them.
- Usage is tracked in `outputs/.budget.json`; its `date` is the Pacific calendar date used for quota rollover. A new date is written immediately on startup, even if the run only hits cache.
- To replay a previous run without any requests, use `--offline` with the same inputs.

The conservative profile is `MAX_CALLS_PER_RUN=10`, `MAX_ORGS=20`, `MAX_CANDIDATES=40`, `BATCH_ORGS=5`, `MAX_SNIPPET_FALLBACKS=2`, `MIN_ORGS=5`, and at most one re-plan round. The code hard-caps each run at 10 accounted Gemini responses even if an older `.env` asks for more. Re-planning consumes a call only when usable official/archived page yield is below target. Usage already spent today still counts; search/page caches reduce repeat work. If you already have `.env`, update the relevant settings manually; `.env` is local and git-ignored.

## Tests

These run offline and make no API calls:

```powershell
python -m tests.test_queries
python -m tests.test_replan
python -m tests.test_discovery
python -m tests.test_crawl
python -m tests.test_step3_people
python -m tests.test_step3_roles
python -m tests.test_step3_cli
python -m tests.test_step4_contacts
python -m tests.test_step5_export
python -m tests.test_small_sample
python -m tests.test_search_cache
python -m tests.test_fetch
python -m tests.test_discovery_seeds
python -m tests.test_location
python -m tests.test_budget
```

## Project layout

```
main.py                      CLI entry point
agent/
  gemini_client.py           the only file that calls the Gemini API
  budget.py                  request and token budget tracking
  prompts.py                 prompt templates
  schemas.py                 data models
  step1_input.py             prompt and PDF to SearchPlan, query builder
  search.py                  web search with caching
  filters.py                 domain, file-type and listicle pre-filters
  location.py                shared city-alias matching
  classify.py                Gemini batch classification with grounding check
  orgs.py                    organization records, root-domain dedupe
  crawl.py                   homepage link scoring
  fetch.py                   page fetch with failure ladder
  replan.py                  re-plan query proposals and validation
  discovery.py               discovery loop
  step3_people.py            decision-maker extraction, grounding and ranking
  step4_contacts.py          code-only contact extraction and verification
  step5_export.py            validation, deduplication and local/optional Sheets export
scripts/
  check_models.py            checks which models your key can use
  make_sample_brochure.py    creates data/sample_brochure.pdf
data/
  sample_brochure.pdf        test brochure
  role_keywords.json         editable decision-maker role priorities
  generic_inboxes.txt        local-parts rejected as shared inboxes
tests/                       offline tests
outputs/                     caches, usage, trace, lead_all.csv, leads.csv, audit_log.csv and leads.db (git-ignored)
```

## Known limitations

- **Scanned PDFs** (image-only) are not supported yet. The program logs a warning and uses only regex seeds.
- **Some company sites block automated access** (HTTP 403) or are slow. These fall back to the Wayback Machine or search snippets. We don't bypass bot protection.
- **Snippet-only person candidates** are allowed only through Step 3's capped same-domain fallback, are scored below page-grounded candidates, and retain the snippet URL as their evidence source.
- **LinkedIn, Instagram and Facebook** are not scraped. Their terms and login walls make them unreliable. Social links found on official sites are recorded instead.
- **DuckDuckGo** returns few results for long or unusual queries, which is why queries are kept short.
- **The prompt path** returns no seed organizations. Seeds come only from a brochure.

## Security

- Never commit `.env`. It holds your API key.
- Never print the key in a terminal, screenshot or chat. Check that it is set with `if ($env:GEMINI_API_KEY) { "key is set" } else { "key is missing" }`.
- Never enable billing on the Google AI Studio or Google Cloud project for this agent.
- Keep `.env`, `credentials.json`, `outputs/` and `.venv/` out of git. Check with `git status --short` before each push.