"""Link scoring for crawling without LLM.
Parses HTML and ranks URLs based on keyword heuristics.
"""

import logging
from urllib.parse import urljoin, urlparse, urlunparse
from typing import List, Tuple

from bs4 import BeautifulSoup

_logger = logging.getLogger(__name__)

# Scoring rules: each tuple contains keywords and an associated score.
SCORE_RULES: List[Tuple[Tuple[str, ...], int]] = [
    # Contact pages are especially valuable for official email and address
    # evidence; rank them above team/about/CSR pages when the page cap is tight.
    (("contact", "contact-us", "contactus", "reach-us", "get-in-touch"), 7),
    (("team", "leadership", "management", "people", "board", "council", "office-bearers"), 5),
    (("sponsor", "partners", "csr", "esg", "sustainability", "governance"), 4),
    (("about", "who-we-are", "committee", "society", "students"), 3),
    (("press", "media"), 2),
]

SKIP_KEYWORDS = (
    "login",
    "careers",
    "career",
    "privacy",
    "terms",
    "blog",
    "cart",
)

SKIP_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".zip",
    ".doc",
    ".docx",
)


def _clean_hostname(url: str) -> str:
    """Return hostname without leading www., lower‑cased, or empty string."""
    hostname = urlparse(url).hostname or ""
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def score_links(html: str, base_url: str, max_links: int = 5) -> List[Tuple[str, int]]:
    """Score <a> links in *html* relative to *base_url*.

    Returns up to *max_links* tuples of (url, score) sorted by descending score.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:
        _logger.warning("Failed to parse HTML: %s", exc)
        return []

    base_host = _clean_hostname(base_url)
    scores: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # Build absolute URL and strip fragment
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        normalized_path = parsed.path.rstrip("/") or "/"
        abs_url = urlunparse(parsed._replace(path=normalized_path, fragment=""))
        # Hostname must match base (ignore leading www.)
        if _clean_hostname(abs_url) != base_host:
            continue
        # Skip unwanted extensions
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        # Skip if any skip keyword appears in the full URL
        lower_url = abs_url.lower()
        if any(kw in lower_url for kw in SKIP_KEYWORDS):
            continue
        # Build text to score
        link_text = a.get_text(separator=" ", strip=True)
        text_to_score = f"{link_text} {parsed.path}".lower()
        link_score = 0
        for keywords, rule_score in SCORE_RULES:
            if any(kw in text_to_score for kw in keywords):
                link_score += rule_score
        if link_score == 0:
            continue
        # Keep highest score for each URL
        if abs_url not in scores or link_score > scores[abs_url]:
            scores[abs_url] = link_score

    # Sort and limit
    sorted_links = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return sorted_links[:max_links]

__all__ = ["score_links", "SCORE_RULES", "SKIP_KEYWORDS", "SKIP_EXTENSIONS"]
