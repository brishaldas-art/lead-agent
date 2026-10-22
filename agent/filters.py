"""Pre‑filter search results before any Gemini calls.
Removes unwanted domains, file types, listicle pages, and directories.
Keeps original ordering for kept items.
All logic is pure standard‑library.
"""

import re
from urllib.parse import urlparse
from pathlib import Path
from typing import List, Dict, Tuple

# --- Configuration -----------------------------------------------------------

BLOCKED_DOMAINS: set[str] = {
    "linkedin.com",
    "facebook.com",
    # Search/ad redirectors are not organization websites. In particular,
    # SERP ad click URLs must not become the candidate's root domain.
    "bing.com",
    "google.com",
    "duckduckgo.com",
    "search.yahoo.com",
    "doubleclick.net",
    "googleadservices.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "pinterest.com",
    "wikipedia.org",
    "glassdoor.co.in",
    "ambitionbox.com",
    "justdial.com",
    "indiamart.com",
    "clutch.co",
    "goodfirms.co",
    # Company directories, corporate-data aggregators and product catalogs
    # may help discover a name, but must not be used as the lead's homepage.
    "zaubacorp.com",
    "findglocal.com",
    "thecompanycheck.com",
    "companydetails.in",
    "infoqik.com",
    "enfsolar.com",
    "corporatedir.com",
    "tofler.in",
    "mca.gov.in",
    "crunchbase.com",
    "tracxn.com",
    "timesofindia.indiatimes.com",
    "economictimes.indiatimes.com",
    "livemint.com",
    "moneycontrol.com",
    "ndtv.com",
    "inc42.com",
}

BLOCKED_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".zip",
    ".doc",
    ".docx",
)

# List‑item patterns (listicles)
LISTICLE_PATTERNS: List[re.Pattern] = [
    re.compile(r"top \d+", re.I),
    re.compile(r"best \d+", re.I),
    re.compile(r"\bbest\b.*\bin\b", re.I),
    re.compile(r"\blist of\b", re.I),
    re.compile(r"\bdirectory\b", re.I),
    re.compile(r"companies in", re.I),
    re.compile(r"\bvs\b", re.I),
    re.compile(r"\breviews?\b", re.I),
]

# Separate directory title check (different drop reason)
DIRECTORY_PATTERN = re.compile(r"\bdirectory\b", re.I)

_logger = __import__("logging").getLogger("agent")


def _is_blocked_domain(hostname: str) -> bool:
    """Return True if *hostname* matches a blocked domain pattern."""
    if not hostname:
        return False
    hostname = hostname.lower()
    for domain in BLOCKED_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _has_blocked_extension(url: str) -> bool:
    """Return True if *url* ends with a blocked file extension."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS)


def filter_results(results: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split *results* into kept and dropped items with reasons.

    Order of checks: blocked domain → file type → listicle → directory title.
    The first matching reason determines the drop.
    """
    kept: List[Dict] = []
    dropped: List[Dict] = []
    for item in results:
        url = item.get("url")
        if not url:
            dropped_item = dict(item)
            dropped_item["drop_reason"] = "no_url"
            dropped.append(dropped_item)
            continue
        hostname = urlparse(url).hostname or ""
        # 1. Blocked domain
        if _is_blocked_domain(hostname):
            dropped_item = dict(item)
            dropped_item["drop_reason"] = "blocked_domain"
            dropped.append(dropped_item)
            continue
        # 2. Blocked extension
        if _has_blocked_extension(url):
            dropped_item = dict(item)
            dropped_item["drop_reason"] = "blocked_filetype"
            dropped.append(dropped_item)
            continue
        title = item.get("title", "")
        # 3. Listicle patterns
        listicle_matched = False
        for pat in LISTICLE_PATTERNS:
            if pat.search(title):
                dropped_item = dict(item)
                dropped_item["drop_reason"] = "listicle"
                dropped.append(dropped_item)
                listicle_matched = True
                break
        if listicle_matched:
            continue
        # 4. Directory title (different reason)
        if DIRECTORY_PATTERN.search(title):
            dropped_item = dict(item)
            dropped_item["drop_reason"] = "directory_title"
            dropped.append(dropped_item)
            continue
        # Keep the item unchanged
        kept.append(item)
    return kept, dropped

__all__ = ["filter_results", "BLOCKED_DOMAINS", "BLOCKED_EXTENSIONS", "LISTICLE_PATTERNS"]
