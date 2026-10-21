"""Replan utilities for generating new search queries.
Gemini only proposes wording; this module validates and decides whether to run them.
"""

import logging
import re
from typing import List

from agent.gemini_client import ask
from agent.prompts import REPLAN
from agent.schemas import NewQueries

_logger = logging.getLogger(__name__)

MAX_NEW_QUERIES = 4
MIN_WORDS = 3
MAX_WORDS = 8
URL_PATTERN = re.compile(r"https?://|www\.|\.com\b|\.in\b|\.ac\.in\b|site:", re.IGNORECASE)


def should_replan(usable: int, min_orgs: int, rounds_used: int, max_rounds: int) -> bool:
    """Return True if we have fewer usable orgs than needed and still have rounds left."""
    return usable < min_orgs and rounds_used < max_rounds


def validate_queries(candidates: List[str], used: List[str]) -> List[str]:
    """Clean and filter candidate queries.

    Keeps up to ``MAX_NEW_QUERIES`` entries that satisfy word‑count and URL rules.
    Logs each rejection at DEBUG level.
    """
    kept: List[str] = []
    seen: set[str] = set()
    used_lower = {u.lower() for u in used if isinstance(u, str)}
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        # Normalise whitespace and case
        cleaned = " ".join(raw.split())
        lowered = cleaned.lower()
        # Word count check
        word_count = len(lowered.split())
        if word_count < MIN_WORDS or word_count > MAX_WORDS:
            _logger.debug("reject query '%s': word count %d out of range", cleaned, word_count)
            continue
        # URL pattern check
        if URL_PATTERN.search(lowered):
            _logger.debug("reject query '%s': matches URL pattern", cleaned)
            continue
        # Duplicate against used list
        if lowered in used_lower:
            _logger.debug("reject query '%s': already used", cleaned)
            continue
        # Duplicate within kept
        if lowered in seen:
            _logger.debug("reject query '%s': duplicate in candidates", cleaned)
            continue
        kept.append(cleaned)
        seen.add(lowered)
        if len(kept) >= MAX_NEW_QUERIES:
            break
    return kept


def propose_queries(category: str, city: str | None, used: List[str], usable: int, min_orgs: int) -> List[str]:
    """Ask Gemini for new queries and validate them.

    Parameters
    ----------
    category: "Sponsor" or other.
    city: optional city string.
    used: list of already‑used query strings.
    usable: number of usable orgs discovered so far.
    min_orgs: target number of orgs.
    """
    category_desc = "company" if category == "Sponsor" else "college society"
    prompt = REPLAN.format(
        category=category,
        city=city or "unknown",
        used="\n".join(used) if used else "none",
        found=usable,
        target=min_orgs,
        category_desc=category_desc,
    )
    result: NewQueries | None = ask(
        "gemini-1.5-flash",
        prompt,
        response_schema=NewQueries,
    )
    if result is None:
        _logger.warning("Gemini did not return new queries for replan")
        return []
    return validate_queries(getattr(result, "queries", []), used)

__all__ = ["should_replan", "validate_queries", "propose_queries"]
