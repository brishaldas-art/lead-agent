"""Small, deterministic helpers for grounding organization search results."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from rapidfuzz import fuzz


def is_social_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("linkedin.com", "instagram.com"))


def linkedin_company_name(result: dict) -> str | None:
    """Take a name only from a public LinkedIn company-page result title."""
    url = result.get("url", "")
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if "linkedin.com" not in (parsed.hostname or "").lower() or "/company/" not in path:
        return None
    title = re.sub(r"\s*(?:\||[-–—])\s*linkedin\s*$", "", result.get("title", ""), flags=re.I)
    title = re.sub(r"\s+\|\s*linkedin.*$", "", title, flags=re.I).strip(" -|–—")
    if len(title) < 3 or title.lower() in {"linkedin", "company", "home"}:
        return None
    return title


def instagram_company_name(result: dict) -> str | None:
    """Extract a business-like Instagram handle only from a public profile URL."""
    parsed = urlparse(result.get("url", ""))
    if (parsed.hostname or "").lower() not in {"instagram.com", "www.instagram.com"}:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) != 1:
        return None
    title = result.get("title", "")
    if not re.search(r"\b(solar|energy|renewable|power|company|solutions)\b", title, re.I):
        return None
    handle = re.search(r"@([A-Za-z0-9_.]+)", title)
    if handle:
        return handle.group(1).replace("_", " ").replace(".", " ").strip()
    return None


def listicle_names(snippet: str) -> list[str]:
    """Recover a few explicitly named organizations from common snippet forms."""
    match = re.search(r"\b(?:including|featured)\s*:?\s*(.+)", snippet or "", re.I)
    if not match:
        return []
    segment = re.split(r"[.!?]", match.group(1), maxsplit=1)[0]
    names = [part.strip(" \t\n:;-–—") for part in re.split(r",|\band\b", segment, flags=re.I)]
    return [name for name in names if 2 <= len(name.split()) <= 7 and len(name) <= 70][:4]


def canonical_url_key(url: str) -> str:
    """Drop tracking query/fragments for duplicate-result detection."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/").lower()
    return urlunparse((parsed.scheme.lower(), host, path, "", "", ""))


def result_matches_name(name: str, result: dict, threshold: int = 72) -> bool:
    evidence = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    return fuzz.token_set_ratio(name.lower(), evidence) >= threshold


def social_matches_name(name: str, result: dict, threshold: int = 78) -> bool:
    title = re.sub(r"\s*(?:\||[-–—])\s*(?:linkedin|instagram).*$", "", result.get("title", ""), flags=re.I)
    evidence = f"{title} {result.get('snippet', '')}".lower()
    return fuzz.token_set_ratio(name.lower(), evidence) >= threshold
