"""Utility to build OrgCandidate objects from classified results.
Root domain is the unique identifier; no values are invented.
"""

import logging
from typing import List, Dict

import tldextract
from rapidfuzz import fuzz

from .schemas import OrgCandidate
from .evidence import domain_matches_name

_logger = logging.getLogger(__name__)
# Use tldextract's bundled suffix snapshot. Avoid an unplanned network request
# during a small run; the bundled list is sufficient for normal domain parsing.
_extract_domain = tldextract.TLDExtract(suffix_list_urls=())

NOISE_SUFFIXES = [
    "csr",
    "official site",
    "official",
    "sponsors",
    "sponsor",
    "home",
    "about",
    "welcome",
]

_SEPARATORS = [" - ", " | ", " — ", ":"]


def clean_name(name: str) -> str:
    """Return a cleaned version of *name*.

    Splits on common separators, removes trailing noise words, and strips whitespace.
    If cleaning yields an empty string, the original stripped name is returned.
    """
    original = name.strip()
    # Split on first separator found
    for sep in _SEPARATORS:
        if sep in original:
            original = original.split(sep, 1)[0]
            break
    cleaned = original.strip()
    # Remove trailing noise suffixes (case‑insensitive)
    lowered = cleaned.lower()
    changed = True
    while changed:
        changed = False
        for suffix in NOISE_SUFFIXES:
            if lowered.endswith(" " + suffix) or lowered == suffix:
                # Remove the suffix and any preceding space
                cleaned = cleaned[: -len(suffix)].strip()
                lowered = cleaned.lower()
                changed = True
                break
    return cleaned if cleaned else name.strip()


def build_orgs(classified: List[Dict], discovered_via: str = "search") -> List[OrgCandidate]:
    """Create :class:`OrgCandidate` objects from classified results.

    Parameters
    ----------
    classified: List of dicts with keys ``org_name``, ``category``, ``city``, ``url``,
        ``title`` and ``snippet``.
    discovered_via: Tag indicating how the org was discovered (default ``"search"``).
    """
    seen_domains: Dict[str, OrgCandidate] = {}
    results: List[OrgCandidate] = []

    for item in classified:
        url = item.get("url", "") or ""
        if not url:
            _logger.warning("Skipping org with missing URL: %s", item.get("org_name"))
            continue
        ext = _extract_domain(url)
        if not ext.suffix:
            _logger.warning("Skipping URL without suffix: %s", url)
            continue
        domain = f"{ext.domain}.{ext.suffix}"
        if domain in seen_domains:
            continue
        # Clean organization name and reject directory/search-result hosts that
        # do not resemble the company's brand or initials. The URL is evidence,
        # not an official homepage, unless this consistency check passes.
        cleaned_name = clean_name(item.get("org_name", ""))
        if not domain_matches_name(cleaned_name, url):
            _logger.warning(
                "Skipping mismatched host %s for organization %s",
                url,
                cleaned_name,
            )
            continue
        # Deduplicate by similar name
        duplicate = False
        for cand in results:
            if fuzz.token_sort_ratio(cleaned_name.lower(), cand.org_name.lower()) >= 90:
                _logger.warning(
                    "Duplicate organization name detected: %s (skipping)", cleaned_name
                )
                duplicate = True
                break
        if duplicate:
            continue
        # Build homepage URL from extracted domain (strip leading www.)
        host = ext.fqdn
        if host.startswith("www."):
            host = host[4:]
        homepage = f"https://{host}"
        candidate = OrgCandidate(
            org_name=cleaned_name,
            category=item.get("category"),
            city=item.get("city"),
            domain=domain,
            homepage=homepage,
            discovered_via=discovered_via,
        )
        seen_domains[domain] = candidate
        results.append(candidate)
    return results

__all__ = ["clean_name", "build_orgs"]
