# Lead Discovery Agent

An agent that takes a search prompt, an event brochure PDF, or both, and finds sponsors and college delegations. It reads public web pages, identifies organizations, and will (in later parts) name a decision-maker and a real contact for each one. The final output is a Google Sheet of verified leads.

The agent never invents data. Gemini is used only for language tasks. URLs, domains, emails and grounding checks are done in code.

## Status

| Part | Description | Status |
|---|---|---|
| 1 | Input processing: prompt and PDF to `SearchPlan` | Done |
| 2 | Lead discovery: search, filter, classify, crawl, re-plan | Implemented. Offline tests pass. Live run verification in progress |
| 3 | Decision-maker identification | Not started |
| 4 | Contact extraction and verification (code only) | Not started |
| 5 | Export to Google Sheets and CSV | Not started |

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
- `OFFLINE=1` while developing, so nothing reaches the API

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

This runs up to 3 broad searches, 2 public social searches, and up to 7 exact-name lookups seeded from public LinkedIn/Instagram snippets or names explicitly present in list-result snippets. Social URLs are never treated as organization websites; list-result snippets only seed a separate company-name lookup. At most 30 non-social results are classified in one batch; output is capped at 3 organizations and 2 pages per organization. It writes evidence, verification flags, status codes, and token usage to `outputs/small_sample.json`.

The corresponding research brief is in `data/sponsor_search_brief.txt`. Company-directory hosts are filtered as discovery sources, and a candidate homepage must also resemble the organization's brand or initials. Public social snippets can seed a name lookup (including business-like Instagram post titles), but are not treated as company-site evidence. Location evidence, direct sponsorship wording, and weaker CSR/community signals are reported separately; CSR keywords alone do not count as sponsorship, and absence of a signal is not proof that an organization is unsuitable.

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
MAX_ORGS=8
BATCH_ORGS=3
MAX_CALLS_PER_RUN=12
REPLAN_ROUNDS=1
OFFLINE=1
```

Each model name in `MODEL_FAST` and `MODEL_SMART` is tried in order. When one model reaches its daily limit, the next one in the chain is used.

## Quota and caching

- The free tier allows about 20 requests per model per day. Limits reset at midnight Pacific Time.
- Limits apply per Google Cloud project, not per key.
- Every Gemini answer and every search result is cached in `outputs/.llm_cache`. A repeated input makes no new request.
- Usage is tracked in `outputs/.budget.json`. Check it before a live run.
- To replay a previous run without any requests, use `--offline` with the same inputs.

## Tests

These run offline and make no API calls:

```powershell
python -m tests.test_queries
python -m tests.test_replan
python -m tests.test_discovery
python -m tests.test_crawl
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
scripts/
  check_models.py            checks which models your key can use
  make_sample_brochure.py    creates data/sample_brochure.pdf
data/
  sample_brochure.pdf        test brochure
  blocked_domains.txt        domains excluded from search results
tests/                       offline tests
outputs/                     cache, usage state and trace log (git-ignored)
```

## Known limitations

- **Scanned PDFs** (image-only) are not supported yet. The program logs a warning and uses only regex seeds.
- **Some company sites block automated access** (HTTP 403) or are slow. These fall back to the Wayback Machine or search snippets. We don't bypass bot protection.
- **Snippet-only organizations** are not counted as usable for Part 3.
- **LinkedIn, Instagram and Facebook** are not scraped. Their terms and login walls make them unreliable. Social links found on official sites are recorded instead.
- **DuckDuckGo** returns few results for long or unusual queries, which is why queries are kept short.
- **The prompt path** returns no seed organizations. Seeds come only from a brochure.

## Security

- Never commit `.env`. It holds your API key.
- Never print the key in a terminal, screenshot or chat. Check that it is set with `if ($env:GEMINI_API_KEY) { "key is set" } else { "key is missing" }`.
- Never enable billing on the Google AI Studio or Google Cloud project for this agent.
- Keep `.env`, `credentials.json`, `outputs/` and `.venv/` out of git. Check with `git status --short` before each push.