# Lead Discovery Agent

An agent that takes a search prompt, an event brochure PDF, or both, and finds sponsors and college delegations. It reads public web pages, identifies organizations, and grounds a decision-maker for each usable lead. Contact verification and local CSV/SQLite export are implemented; Google Sheets export is optional when local service-account credentials are configured. The final goal is a sheet of verified leads.

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

The corresponding research brief is in `data/sponsor_search_brief.txt`. Company-directory hosts are filtered as discovery sources, and a candidate homepage must also resemble the organization's brand or initials. Public social/list snippets can seed name lookups (including business-like Instagram post titles), but are not treated as company-site evidence. The normal `main.py --prompt` Sponsor/Both discovery uses the same bounded enrichment by default: up to two public social searches, seven exact-name lookups, and a 30-result classification cap when verified lookup results are available. Social/list sources are returned under a separately labeled `discovery_evidence` field and printed as discovery-only evidence. Location evidence, direct sponsorship wording, and weaker CSR/community signals are reported separately; CSR keywords alone do not count as sponsorship, and absence of a signal is not proof that an organization is unsuitable.

## How it works

### Part 1: Input processing

1. **Prompt path:** one Gemini call extracts category, city and industries from the request.
2. **PDF path:** PyMuPDF extracts the text, which uses no tokens. One Gemini call reads the event facts. Organization names from Gemini are kept only if they appear in the PDF text, so invented names are dropped. A heading scan also picks up names under headings such as "Past Sponsors".
3. **Queries:** built from code templates, never from Gemini. Event hosts are excluded from the seed organizations.

### Part 2: Lead discovery

1. **Search:** each query runs through DuckDuckGo (`ddgs`). Results are cached.
2. **Filter (code, no tokens):** results from directories, news sites, social networks, listicles, event agencies, sponsorship agencies, and blocked domains in `data/blocked_domains.txt` are dropped. Results whose title or snippet contains words such as "foundation", "trust", "university" or "agency" are also dropped.
3. **Classify (Gemini, batched):** up to 30 results per call. Gemini returns whether each result is a relevant company in the requested industry. Code then checks that the organization name appears in that result's title or snippet.
4. **Organizations:** one record per root domain. The URL and domain come from the search result, never from Gemini.
5. **Crawl:** the homepage is fetched and links are scored by keyword (team, about, leadership, contact, sponsors, CSR and similar). The best few pages are fetched, to a depth of 2.
6. **Failure ladder:** a failed page is retried, then tried at an alternate URL (http/https, with or without www), then loaded from the Wayback Machine. If all of these fail, the organization falls back to its search snippet and is marked as not usable.
7. **Re-plan (at most one round):** if too few organizations are usable, Gemini proposes up to 4 new search queries. Code rejects any query that is longer than 5 words, contains a URL, repeats an earlier query, or contains event words for a company run.
8. **Stop conditions:** the run ends when it reaches `MAX_ORGS`, the call budget (`MAX_CALLS_PER_RUN`) or the re-plan limit.

### Part 3: Decision-maker identification

After discovery, each usable organization is checked against the editable priority roles in `data/role_keywords.json`. In city-scoped runs, organizations without matching live-page location evidence are skipped before Step 3 to avoid spending model calls on leads Step 5 must reject. Step 3 builds merged windows around role keywords, labels every window with its original page URL, and caps context at 6,000 characters per organization. It batches up to `BATCH_ORGS` organizations per Gemini `smart` request.

Gemini only proposes a name, role, organization ID, and source URL. Code rejects candidates unless the person name is present in the original source text (fuzzy score at least 92), a matching target role is supported within 300 characters, and the returned URL exactly matches a labelled page. Candidates are ranked by role priority, page type, and live-versus-Wayback source; the highest-ranked person is selected and the others are retained for audit. Generic entries and prompt-injected claims are not accepted without source grounding.

If no grounded person is found, Step 3 may search the organization's own domain for the highest-priority role and inspect same-domain result snippets. This fallback is limited by `MAX_SNIPPET_FALLBACKS` (default 5); snippet-only sources receive a lower score. Organizations without a grounded person are reported as dropped with a reason. Step 3 never fabricates a name or visits LinkedIn to infer employment.

The offline Step 3 tests use fake model and search functions, so they make **zero API calls and zero live searches**:

```powershell
python -m tests.test_step3_people
python -m tests.test_step3_roles
python -m tests.test_step3_cli
python -m tests.test_step4_contacts
```

### Part 4: Contact extraction and verification

Step 4 is code-only; Gemini never generates or verifies a contact. It scans live official-domain HTML/text for `mailto:` and plain emails, de-obfuscates common `[at]`/`[dot]` forms, and decodes Cloudflare `data-cfemail`. Generic inboxes and emails outside the organization domain are rejected. An email is `Verified` only when it was found on a live official page, its local part matches the Step 3 person, its syntax passes, and the email domain has MX records.

If no exact personal email qualifies, the agent may derive an address only from a dominant same-domain format observed in at least two distinct personal-looking addresses. These are labeled `Pattern-Derived`, never `Verified`. As a final fallback, profile links must be present in the official page HTML, return HTTP 200 on a single ordinary request, and match the person or organization. No login, retries, SMTP probing, or bot-protection bypass is used. Wayback-only contact data is not considered current verification. Each accepted contact retains its evidence/supporting URLs and passed checks in the result and CLI output.

Offline tests inject fake MX and profile responses and make **no DNS, HTTP, API, or live search calls**:

```powershell
python -m tests.test_step4_contacts
```

### Part 5: Validation and export

Step 5 consumes only Step 4 verified contacts and makes **zero Gemini calls**. It validates the exact six-column row, requires the organization city to appear in a live official-domain page, deduplicates on root domain/contact (with fuzzy organization-name matching), and records row-level drop reasons. It always writes `outputs/leads.csv`, `outputs/leads.db`, and `outputs/audit_log.csv` before any optional Sheets connection. The main CSV/sheet has exactly six columns; source URLs and checks stay in the audit outputs.

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
MAX_ORGS=6
BATCH_ORGS=3
MAX_CALLS_PER_RUN=10
MAX_SNIPPET_FALLBACKS=2
REPLAN_ROUNDS=0
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

The safe profile is `MAX_CALLS_PER_RUN=10`, `MAX_ORGS=6`, `MAX_SNIPPET_FALLBACKS=2`, and `REPLAN_ROUNDS=0`. The code hard-caps each run at 10 accounted Gemini responses even if an older `.env` asks for more. This aims to allow two full runs within the typical 20-requests-per-model daily limit; usage already spent today still counts, and actual call needs vary by prompt/cache/results. The saved budget file is intentionally retained across runs and rolls over at midnight Pacific. If you already have `.env`, lower its `MAX_ORGS`, fallback, and replan settings to match the safe profile; `.env` is local and git-ignored.

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
  filters.py                 drops directories, news, agencies, blocked domains
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
  blocked_domains.txt        domains excluded from search results
  role_keywords.json         editable decision-maker role priorities
  generic_inboxes.txt        local-parts rejected as shared inboxes
tests/                       offline tests
outputs/                     cache, usage, trace, leads.csv, audit_log.csv and leads.db (git-ignored)
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