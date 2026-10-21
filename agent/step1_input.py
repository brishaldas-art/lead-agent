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

HEADING_PAT = re.compile(
    r"^\s*(Our Sponsors|Our Past Sponsors|Past Sponsors|Previous Sponsors|"
    r"Past Partners|Participating Colleges)\s*$",
    re.IGNORECASE,
)
NAME_PAT = re.compile(r"^[A-Z].{2,59}$")


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


def plan_from_prompt(prompt: str) -> SearchPlan:
    """Build a SearchPlan from a free-form user prompt using Gemini (fast tier)."""
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
        _log.warning("Scanned PDF fallback not implemented yet: %s", path)
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


def build_queries(plan: SearchPlan) -> SearchPlan:
    """Fill plan.queries from code templates. No LLM, no URLs. Capped at 12."""
    city = plan.city or "India"
    theme = plan.theme or "college fest"
    queries: List[str] = []

    if plan.category in ("Sponsor", "Both"):
        for ind in plan.industries or ["technology"]:
            queries.append(f"{ind} companies sponsor college fests {city}")
        queries.append(f"{city} college fest sponsors")
        for seed in plan.seed_orgs[:3]:
            queries.append(f"{seed} CSR head")
            queries.append(f"{seed} marketing head")

    if plan.category in ("College Delegation", "Both"):
        colleges = [s for s in plan.seed_orgs if any(t in s for t in ("College", "Institute", "University"))][:3]
        if colleges:
            queries.append(f"{city} engineering colleges technical society")
            queries.append(f"{city} college cultural fest team")
        for college in colleges:
            queries.append(f"{college} student council")

    final: List[str] = []
    for q in queries:
        if q not in final:
            final.append(q)
    return plan.model_copy(update={"queries": final[:12]})


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
    return build_queries(merged)


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