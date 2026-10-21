# Lead Discovery Agent

An agent that takes a search prompt, an event brochure PDF, or both, and produces a structured plan for finding sponsors and college delegations. The final goal is a Google Sheet of verified leads, each with a named decision-maker and a real contact.

This README covers **Part 1 (Input Processing)** only. Later parts are not yet implemented.

## Status

| Part | Description | Status |
|---|---|---|
| 1 | Input processing: prompt and PDF to `SearchPlan` | Done |
| 2 | Lead discovery (search, crawl, classify) | Not started |
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

- `GEMINI_API_KEY` to your key from https://aistudio.google.com/app/apikey
- `MODEL_FAST` and `MODEL_SMART` to model names that your key accepts

Then check which models work. This sends one small request per model:

```powershell
python scripts\check_models.py
```

Each name should print `WORKS`. Remove any name that prints `FAILS` or `MISSING`.

## Run

Input is one of three: a prompt, a PDF, or both.

```powershell
# Prompt only (1 Gemini request)
python main.py --prompt "Find tech sponsors in Bengaluru for a 2000-person college fest"

# PDF brochure only (1 Gemini request)
python main.py --pdf data\sample_brochure.pdf

# Both
python main.py --prompt "Find sponsors for this fest" --pdf data\sample_brochure.pdf

# Offline: no Gemini calls, uses regex and templates only
python main.py --offline --pdf data\sample_brochure.pdf
```

The program prints a plan with these fields:

- **Source**: `prompt`, `pdf`, or `both`
- **Category**: `Sponsor`, `College Delegation`, or `Both`
- **City**, **Theme**, **Industries**
- **Seed orgs**: organizations found in the brochure text
- **Queries**: up to 12 search strings for Part 2

Running with no arguments prints a usage message and exits with code 1.

## How it works

1. **Prompt path:** one Gemini call extracts category, city, and industries from the request.
2. **PDF path:** PyMuPDF extracts the text (no tokens used). One Gemini call reads the event facts. The seed organizations are then checked against the PDF text, so any name Gemini invents is dropped. A regex also reads names under headings such as "Past Sponsors".
3. **Queries:** built from code templates, never from Gemini. Event hosts are excluded from seeds.

## Quota and caching

- The free tier allows about 20 requests per model per day, and the limits reset at midnight Pacific Time.
- Every answer is cached in `outputs/.llm_cache`. Repeating the same input makes no new request.
- Usage is tracked in `outputs/.budget.json`.
- Use `--offline` while developing to avoid spending requests.

## Project layout (Part 1)

```
main.py                    CLI entry point
agent/
  gemini_client.py         the only file that calls the Gemini API
  budget.py                request and token budget tracking
  prompts.py               prompt templates
  schemas.py               data models
  step1_input.py           prompt and PDF to SearchPlan
scripts/
  check_models.py          checks which models your key can use
  make_sample_brochure.py  creates data/sample_brochure.pdf
data/
  sample_brochure.pdf      test brochure
outputs/                   cache and usage state (git-ignored)
```

## Known limitations

- Scanned PDFs (image-only) are not supported yet. The program logs a warning and uses only the regex seeds.
- Gemini is used only for language tasks. It never writes URLs or email addresses.
- The prompt path returns no seed organizations. Seeds come only from a brochure.

## Security

- Never commit `.env`. It holds your API key.
- Never enable billing on the Google AI Studio or Google Cloud project for this agent.