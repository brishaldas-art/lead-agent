"""Replan: ask Gemini for a few new search queries, then validate them in code.

Gemini only proposes wording. Code decides which queries are allowed to run.
"""

import logging
import re
from typing import Iterable, List, Optional, Pattern

from agent.gemini_client import ask
from agent.prompts import REPLAN
from agent.schemas import NewQueries

_log = logging.getLogger("agent")

MAX_NEW_QUERIES = 4
MIN_WORDS = 3
MAX_WORDS = 5  # Keep validation aligned with the prompt; search phrases stay concise.

URL_PATTERN = re.compile(r"https?://|www\.|\.(com|in|org|net|edu)\b|site:", re.IGNORECASE)

# Company runs must not carry event or college wording (see step1_input.py).
COMPANY_BANNED = re.compile(
    r"\b(sponsor\w*|college|colleges|fest|fests|delegation|student|students)\b",
    re.IGNORECASE,
)


def should_replan(usable: int, min_orgs: int, rounds_used: int, max_rounds: int) -> bool:
    """True if usable orgs are below target and re-plan rounds remain."""
    return usable < min_orgs and rounds_used < max_rounds


def _category_desc(category: str) -> str:
    """Company for Sponsor and Both runs, college society for College Delegation."""
    return "college society" if category == "College Delegation" else "company"


def validate_queries(
    candidates: Iterable[str],
    used: Iterable[str],
    banned: Optional[Pattern[str]] = None,
) -> List[str]:
    """Clean and filter candidate queries. Returns at most MAX_NEW_QUERIES.

    Rules: 3 to 5 words, no URL-like text, no repeat of a used or kept query,
    and no banned words when ``banned`` is given.
    """
    kept: List[str] = []
    seen = {u.lower() for u in used if isinstance(u, str)}

    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cleaned = " ".join(raw.split())
        lowered = cleaned.lower()
        word_count = len(lowered.split())

        if not (MIN_WORDS <= word_count <= MAX_WORDS):
            _log.debug("replan reject '%s': %d words", cleaned, word_count)
            continue
        if URL_PATTERN.search(lowered):
            _log.debug("replan reject '%s': URL-like", cleaned)
            continue
        if banned is not None and banned.search(cleaned):
            _log.debug("replan reject '%s': banned word", cleaned)
            continue
        if lowered in seen:
            _log.debug("replan reject '%s': duplicate", cleaned)
            continue

        kept.append(cleaned)
        seen.add(lowered)
        if len(kept) >= MAX_NEW_QUERIES:
            break

    return kept


def propose_queries(
    category: str,
    city: Optional[str],
    used: List[str],
    usable: int,
    min_orgs: int,
) -> List[str]:
    """Ask Gemini for new queries and validate them. Returns [] on failure."""
    prompt = REPLAN.format(
        category=category,
        city=city or "unknown",
        used="\n".join(used) if used else "none",
        found=usable,
        target=min_orgs,
        category_desc=_category_desc(category),
    )

    result = ask(NewQueries, prompt, tier="fast", tag="s2_replan", max_out=400)
    if result is None:
        _log.warning("replan: Gemini returned no queries")
        return []

    banned = None if category == "College Delegation" else COMPANY_BANNED
    return validate_queries(getattr(result, "queries", []), used, banned=banned)


__all__ = ["should_replan", "validate_queries", "propose_queries"]