"""Small, deterministic helpers for grounding organization search results."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

import tldextract
from rapidfuzz import fuzz

_SUFFIX_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


def is_social_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("linkedin.com", "instagram.com"))


def domain_matches_name(name: str, url: str) -> bool:
    """Reject search/listing hosts that are unrelated to the named company.

    This is a conservative consistency check, not proof of domain ownership.
    A brand token or the organization's initials must occur in the registrable
    domain; otherwise the URL remains evidence only and is not its homepage.
    """
    ext = _SUFFIX_EXTRACTOR(url)
    domain = (ext.domain or "").lower()
    if not domain:
        return False
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    legal = {"the", "private", "pvt", "limited", "ltd", "llp", "inc", "corp", "corporation", "company", "co"}
    generic = {"renewable", "energy", "solar", "power", "green", "solutions", "services", "systems", "technology", "technologies", "india", "west", "bengal", "kolkata"}
    tokens = [token for token in tokens if token not in legal]
    if any(len(token) >= 3 and token not in generic and token in domain for token in tokens):
        return True
    # Generic multiword brands (for example, "Green Energy") often use the
    # joined phrase in their domain. Require the full phrase, not one generic
    # word, so a directory host cannot pass on a weak substring.
    joined_name = "".join(tokens)
    if len(tokens) >= 2 and len(joined_name) >= 6 and joined_name in domain:
        return True
    initials = "".join(token[0] for token in tokens if token)
    return len(initials) >= 3 and initials in domain


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
    """Extract a business-like name from a public business profile/post snippet."""
    parsed = urlparse(result.get("url", ""))
    if (parsed.hostname or "").lower() not in {"instagram.com", "www.instagram.com"}:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts or parts[0].lower() in {"popular", "explore", "stories"}:
        return None
    title = result.get("title", "")
    snippet = result.get("snippet", "") or ""
    handle = re.search(r"@([A-Za-z0-9_.]+)", title)
    if len(parts) == 1 and handle:
        if re.search(r"\b(solar|energy|renewable|power|company|solutions)\b", title, re.I):
            return handle.group(1).replace("_", " ").replace(".", " ").strip()
    # Public post/reel results can carry a company name and location even when
    # the profile itself is not indexed. Avoid personal-name titles by requiring
    # a company/sector cue in the title prefix or a matching cue in the snippet.
    prefix = re.split(r"\s*(?:\||[-–—])\s*", title, maxsplit=1)[0].strip()
    if not prefix or prefix.lower() in {"instagram", "facebook"}:
        return None
    cues = re.compile(r"\b(solar|energy|renewable|power|company|solutions|solar\s+plant)\b", re.I)
    if cues.search(prefix) or (prefix.lower() in snippet.lower() and cues.search(snippet)):
        return prefix
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


def summarize_official_evidence(domain: str, search_results: list[dict], pages: list[dict], city: str | None) -> dict:
    """Report local, sponsorship, and related-CSR signals from company-domain evidence only."""
    evidence = []
    for row in search_results:
        url = row.get("url", "")
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == domain or host.endswith("." + domain):
            evidence.append({
                "url": url,
                "text": f"{row.get('title', '')} {row.get('snippet', '')}",
            })
    for page in pages:
        url = page.get("url", "")
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == domain or host.endswith("." + domain):
            evidence.append({"url": url, "text": page.get("text", "") or ""})

    direct_sponsor_pattern = re.compile(
        r"\b(sponsorships?|sponsored\s+(?:events?|programs?|students?|colleges?|fests?)|"
        r"sponsor(?:s|ed|ing)?\s+(?:the\s+)?(?:(?:college|school|university)\s+)?"
        r"(?:events?|programs?|students?|colleges?|fests?|festivals?))\b",
        re.I,
    )
    related_signal_pattern = re.compile(
        r"\b(csr|esg|education|student(?:s)?|college|community|grant|donation|"
        r"funding|scholarship|outreach|youth)\b",
        re.I,
    )
    city_pattern = re.compile(rf"(?<!\w){re.escape(city.strip())}(?!\w)", re.I) if city else None
    location_sources, direct_sponsor_sources, related_signal_sources = [], [], []
    for item in evidence:
        text = item["text"]
        if city_pattern and city_pattern.search(text):
            location_sources.append(item["url"])
        if direct_sponsor_pattern.search(text):
            direct_sponsor_sources.append(item["url"])
        if related_signal_pattern.search(text):
            related_signal_sources.append(item["url"])

    location_sources = list(dict.fromkeys(url for url in location_sources if url))
    direct_sponsor_sources = list(dict.fromkeys(url for url in direct_sponsor_sources if url))
    related_signal_sources = list(dict.fromkeys(url for url in related_signal_sources if url))
    if location_sources and direct_sponsor_sources:
        status = "local_presence_and_direct_sponsorship_evidence"
    elif location_sources and related_signal_sources:
        status = "local_presence_and_related_csr_signal_only"
    elif location_sources:
        status = "local_presence_verified_no_sponsor_signal"
    else:
        status = "location_unverified"
    return {
        "status": status,
        "target_city": city,
        "location_evidence_urls": location_sources,
        "direct_sponsorship_evidence_urls": direct_sponsor_sources,
        "related_csr_community_evidence_urls": related_signal_sources,
        "warning": "Only company-domain evidence is counted. CSR/community keywords are research signals, not proof of sponsorship willingness; human review is required.",
    }
