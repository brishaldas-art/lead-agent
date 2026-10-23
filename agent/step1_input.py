"""Step 1: turn a user prompt and/or a PDF brochure into a SearchPlan."""

import logging
import re
from typing import List, Optional, Tuple

import pymupdf
from rapidfuzz import fuzz

from agent.gemini_client import ask
from agent.prompts import BROCHURE, INTENT
from agent.schemas import BrochureFactsOut, SearchIntentOut, SearchPlan

_log = logging.getLogger("agent")

MAX_QUERY_WORDS = 5   # DuckDuckGo returns little or nothing for longer queries
MAX_QUERIES = 10

HEADING_PAT = re.compile(
    r"^\s*(Our Sponsors|Our Past Sponsors|Past Sponsors|Previous Sponsors|"
    r"Past Partners|Participating Colleges)\s*$",
    re.IGNORECASE,
)
NAME_PAT = re.compile(r"^[A-Z].{2,59}$")

# ---------------------------------------------------------------------------
# Query templates (code only, no LLM)
# ---------------------------------------------------------------------------

# Company runs (Sponsor and Both). Event and college words are never used here.
_INDUSTRY_TEMPLATES = [
    "{ind} companies in {place}",
    "{ind} startups {place}",
]
_DIGITAL_INDUSTRY_TEMPLATES = ["{place} {ind} firms"]
_NON_DIGITAL_INDUSTRY_TEMPLATES = [
    "{place} {ind} manufacturers",
    "{place} {ind} CSR",
]
_DIGITAL_INDUSTRY_TERMS = (
    "software", "it services", "information technology", "technology products",
    "telecommunications", "cybersecurity", "computer", "tech",
)

# College runs (College Delegation only). College wording is expected here.
_COLLEGE_TEMPLATES = [
    "{place} engineering colleges",
    "{place} college technical society",
    "{place} college cultural committee",
    "{place} university student council",
]

_COMPANY_BANNED = re.compile(
    r"\b(sponsor\w*|college|colleges|fest|fests|delegation|student|students)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def extract_pdf_text(path: str) -> Tuple[str, int]:
    """Return all page text joined by newlines, and the page count."""
    doc = pymupdf.open(path)
    try:
        texts = [page.get_text() for page in doc]
        return "\n".join(texts), doc.page_count
    finally:
        doc.close()


def is_scanned(text: str, pages: int) -> bool:
    """Scanned PDFs have very little extractable text per page."""
    return len(text.strip()) < 200 * pages


def grounded(names: List[str], raw: str) -> List[str]:
    """Keep names found in raw text (case-insensitive substring or fuzzy >= 90). Dedupes."""
    raw_lower = raw.lower()
    seen = set()
    result: List[str] = []
    for name in names:
        if not name or name in seen:
            continue
        name_lower = name.lower()
        if name_lower in raw_lower or fuzz.partial_ratio(name_lower, raw_lower) >= 90:
            seen.add(name)
            result.append(name)
    return result


def scan_headings(text: str) -> List[str]:
    """Collect name-like lines that follow a sponsor/partner/college heading."""
    lines = text.splitlines()
    result: List[str] = []
    i = 0
    while i < len(lines):
        if HEADING_PAT.match(lines[i]):
            i += 1
            while i < len(lines):
                cur = lines[i].strip()
                if not cur or HEADING_PAT.match(cur):
                    break
                if NAME_PAT.match(cur) and cur not in result:
                    result.append(cur)
                i += 1
        else:
            i += 1
    return result


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def plan_from_prompt(prompt: str) -> SearchPlan:
    """Build a SearchPlan from a free-form prompt using Gemini (fast tier)."""
    intent = ask(SearchIntentOut, INTENT.format(prompt=prompt), tier="fast", tag="s1_prompt")
    if intent is None:
        intent = SearchIntentOut(category="Both", city=None, industries=[], audience=None)
    return SearchPlan(
        source="prompt",
        category=intent.category,
        city=intent.city,
        industries=intent.industries,
    )


def plan_from_pdf(path: str) -> SearchPlan:
    """Build a SearchPlan from a PDF brochure. Seeds are grounded in the PDF text."""
    text, pages = extract_pdf_text(path)
    facts: Optional[BrochureFactsOut] = None
    if is_scanned(text, pages):
        _log.warning("Scanned PDF: Gemini fallback not implemented, using regex seeds only: %s", path)
    else:
        facts = ask(BrochureFactsOut, BROCHURE.format(text=text[:40000]), tier="fast", tag="s1_brochure")

    candidates: List[str] = []
    if facts is not None:
        candidates.extend(facts.mentioned_orgs)
    candidates.extend(scan_headings(text))
    seed_orgs = grounded(list(dict.fromkeys(candidates)), text)

    first_line = text.strip().splitlines()[0] if text.strip() else ""
    seed_orgs = [s for s in seed_orgs if s not in first_line]

    return SearchPlan(
        source="pdf",
        category="Both",
        city=facts.event_city if facts is not None else None,
        theme=facts.event_theme if facts is not None else None,
        seed_orgs=seed_orgs,
    )


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def _clean_unique(queries: List[str]) -> List[str]:
    """Collapse whitespace and drop duplicates, keeping order."""
    out: List[str] = []
    for q in queries:
        q = " ".join(q.split())
        if q and q not in out:
            out.append(q)
    return out


def _fits(query: str, banned: Optional[re.Pattern] = None) -> bool:
    """True if the query is within the word limit and has no banned words."""
    if len(query.split()) > MAX_QUERY_WORDS:
        _log.info("query dropped (over %d words): %s", MAX_QUERY_WORDS, query)
        return False
    if banned is not None and banned.search(query):
        _log.warning("query dropped (banned word for company run): %s", query)
        return False
    return True


def build_queries(
    category: str,
    city: Optional[str],
    industries: List[str],
    theme: Optional[str],
) -> List[str]:
    """Build search queries from code templates. No LLM is used.

    - College Delegation: college templates only.
    - Sponsor, Both, or anything else: company templates built from the
      industries and city. Event and college words are never added.
    """
    place = city or "India"

    if category == "College Delegation":
        queries = [t.format(place=place) for t in _COLLEGE_TEMPLATES]
        cleaned = _clean_unique(queries)
        return [q for q in cleaned if _fits(q)][:MAX_QUERIES]

    inds = [i.strip() for i in industries if i and i.strip()] or ["technology"]

    queries: List[str] = []
    for ind in inds[:2]:
        normalized_industry = " ".join(ind.casefold().split())
        digital = any(term in normalized_industry for term in _DIGITAL_INDUSTRY_TERMS)
        templates = _INDUSTRY_TEMPLATES + (
            _DIGITAL_INDUSTRY_TEMPLATES if digital else _NON_DIGITAL_INDUSTRY_TEMPLATES
        )
        for template in templates:
            queries.append(template.format(ind=ind, place=place))
    if theme:
        short_theme = " ".join(theme.split()[:2])  # keep long theme phrases from breaking the limit
        queries.append(f"{short_theme} {inds[0]} {place}")

    cleaned = _clean_unique(queries)
    kept = [q for q in cleaned if _fits(q, banned=_COMPANY_BANNED)]
    return kept[:MAX_QUERIES]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_plan(prompt: Optional[str] = None, pdf_path: Optional[str] = None) -> SearchPlan:
    """Build the final Step 1 plan from a prompt, a PDF, or both."""
    if prompt and pdf_path:
        p_plan = plan_from_prompt(prompt)
        pdf_plan = plan_from_pdf(pdf_path)
        merged = SearchPlan(
            source="both",
            category=p_plan.category,
            city=p_plan.city or pdf_plan.city,
            industries=p_plan.industries,
            theme=pdf_plan.theme,
            seed_orgs=list(dict.fromkeys(p_plan.seed_orgs + pdf_plan.seed_orgs)),
        )
    elif prompt:
        merged = plan_from_prompt(prompt)
    elif pdf_path:
        merged = plan_from_pdf(pdf_path)
    else:
        merged = SearchPlan(source="prompt", category="Both", city=None)

    queries = build_queries(merged.category, merged.city, merged.industries, merged.theme)
    _log.info("step1 plan: category=%s city=%s industries=%s queries=%d",
              merged.category, merged.city, merged.industries, len(queries))
    return merged.model_copy(update={"queries": queries})


__all__ = [
    "extract_pdf_text",
    "is_scanned",
    "grounded",
    "scan_headings",
    "plan_from_prompt",
    "plan_from_pdf",
    "build_queries",
    "build_plan",
]