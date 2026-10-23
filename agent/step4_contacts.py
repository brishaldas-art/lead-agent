"""Step 4: extract and verify conservative official-page contact channels.

This module never asks an LLM to write or validate contact data. Person-specific
emails must be published on a live official page, match the grounded person,
pass syntax validation, and have MX records; recognized public email providers
are allowed when the page grounds the matching person. Pattern-derived addresses
require multiple observed examples. Without a verified person-specific channel,
a shared inbox on the company domain or an email published on the official page
from a recognized public provider may qualify as a company contact. Profile links
are accepted only when linked from a live official page and an ordinary HTTP 200
response matches the person or organization. Network functions are injectable.
"""

from __future__ import annotations

import html
import logging
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, HttpUrl
from rapidfuzz import fuzz

from agent.schemas import OrgCandidate
from agent.step3_people import OrgWithPerson, Person, Step3Outcome

logger = logging.getLogger("agent.step4")
ROOT = Path(__file__).resolve().parents[1]
GENERIC_INBOXES_PATH = ROOT / "data" / "generic_inboxes.txt"
USER_AGENT = "Mozilla/5.0 (compatible; LeadAgent/1.0)"
EMAIL_RE = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.I,
)
OBFUSCATED_AT_RE = re.compile(r"\s*(?:\[at\]|\(at\)|\{at\}|\bat\b)\s*", re.I)
OBFUSCATED_DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\bdot\b)\s*", re.I)
GENERIC_DEFAULTS = {
    "info", "information", "contact", "hello", "hi", "support", "admin", "administrator",
    "sales", "enquiry", "enquiries", "inquiry", "office", "mail", "help", "career",
    "careers", "hr", "webmaster", "noreply", "no-reply", "do-not-reply", "postmaster",
    "jobs", "recruitment", "newsletter", "notification", "notifications", "updates",
    "admission", "admissions", "registrar", "accounts", "billing", "press", "media",
    "feedback", "service", "services", "team", "marketing", "communications",
    "partnership", "partnerships", "sponsor", "sponsorship", "business", "general",
    "connect", "corporate", "outreach",
}
HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "professor", "shri", "smt"}
NAME_STOPWORDS = {"the", "of", "for", "and", "at", "in"}
PROFILE_HOSTS = {"linkedin.com", "instagram.com", "facebook.com"}
PROFILE_PATH_BLOCKLIST = {
    "about", "accounts", "explore", "popular", "p", "reel", "reels", "stories",
    "directory", "login", "help", "company", "jobs", "careers",
}
PUBLIC_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com",
    "outlook.com", "outlook.in", "hotmail.com", "hotmail.co.in", "live.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "rediffmail.com", "rediff.com", "protonmail.com",
    "proton.me", "zoho.com", "fastmail.com", "mail.com",
}


class Contact(BaseModel):
    channel: str
    channel_type: Literal["email", "profile_link"]
    status: Literal["Verified", "Pattern-Derived"]
    evidence_url: HttpUrl
    supporting_urls: list[HttpUrl] = Field(default_factory=list)
    checks_passed: list[str] = Field(default_factory=list)


class VerifiedLead(BaseModel):
    org: OrgCandidate
    person: Person
    contact: Contact


@dataclass(frozen=True)
class EmailEvidence:
    address: str
    source_url: str
    page_status: str


@dataclass
class Step4Outcome:
    verified: list[VerifiedLead] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    observed_contacts: list[dict[str, Any]] = field(default_factory=list)


def _ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return normalized.encode("ascii", "ignore").decode("ascii")


def _normalized_name_words(value: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", _ascii(value).casefold())
    while words and words[0] in HONORIFICS:
        words.pop(0)
    return [word for word in words if word not in NAME_STOPWORDS]


def _normalize_email(email: str) -> str:
    return email.strip().strip("<>()[]{}.,;:'\" ").casefold()


def _email_domain(email: str) -> str:
    return _normalize_email(email).rsplit("@", 1)[-1].rstrip(".").lower() if "@" in email else ""


def _same_org_domain(email_domain: str, org_domain: str) -> bool:
    domain = email_domain.casefold().strip(".")
    root = org_domain.casefold().strip(".")
    return bool(domain and root and (domain == root or domain.endswith("." + root)))


def _is_public_email_provider(email_domain: str) -> bool:
    domain = email_domain.casefold().strip(".")
    # Yahoo operates country-specific domains (for example yahoo.co.uk and
    # yahoo.co.in); allow that exact brand namespace, subject to official-page
    # publication and MX verification below.
    if domain.startswith("yahoo."):
        return True
    return any(domain == provider or domain.endswith("." + provider) for provider in PUBLIC_EMAIL_PROVIDERS)


def _load_generic_inboxes(path: Path = GENERIC_INBOXES_PATH) -> set[str]:
    try:
        configured = {
            line.strip().casefold()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError:
        configured = set()
    return configured | GENERIC_DEFAULTS


def is_generic_inbox(email: str, generic_inboxes: set[str] | None = None) -> bool:
    local = _normalize_email(email).split("@", 1)[0].split("+", 1)[0]
    tokens = [token for token in re.split(r"[._-]+", local) if token]
    blocklist = generic_inboxes if generic_inboxes is not None else _load_generic_inboxes()
    normalized_local = re.sub(r"[^a-z0-9]", "", local)
    normalized_blocklist = {re.sub(r"[^a-z0-9]", "", item.casefold()) for item in blocklist}
    return (
        local in blocklist
        or normalized_local in normalized_blocklist
        or bool(tokens and tokens[0] in blocklist)
    )


def decode_cfemail(encoded: str) -> str | None:
    """Decode Cloudflare's documented data-cfemail XOR-hex representation."""
    value = re.sub(r"\s+", "", encoded or "")
    if len(value) < 4 or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return None
    try:
        data = bytes.fromhex(value)
        key = data[0]
        decoded = bytes(byte ^ key for byte in data[1:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if EMAIL_RE.fullmatch(decoded) else None


def _deobfuscate_text(value: str) -> str:
    text = html.unescape(value or "")
    text = OBFUSCATED_AT_RE.sub("@", text)
    text = OBFUSCATED_DOT_RE.sub(".", text)
    return text


def _is_valid_email(address: str) -> bool:
    candidate = _normalize_email(address)
    if not EMAIL_RE.fullmatch(candidate):
        return False
    try:
        from email_validator import EmailNotValidError, validate_email
    except ImportError:
        # Strict local fallback keeps offline tests and minimal installs usable.
        local, domain = candidate.rsplit("@", 1)
        labels = domain.split(".")
        return bool(
            local
            and len(local) <= 64
            and len(candidate) <= 254
            and not local.startswith(".")
            and not local.endswith(".")
            and ".." not in local
            and len(labels) >= 2
            and len(labels[-1]) >= 2
            and all(label and not label.startswith("-") and not label.endswith("-") for label in labels)
        )
    try:
        validate_email(candidate, check_deliverability=False, allow_smtputf8=False)
        return True
    except EmailNotValidError:
        return False


def extract_page_emails(page: dict[str, Any]) -> list[EmailEvidence]:
    """Extract email evidence from visible text, HTML, mailto, and cfemail."""
    url = str(page.get("url", "") or "")
    status = str(page.get("status", "unknown")).casefold()
    html_text = str(page.get("html", "") or "")
    visible_text = str(page.get("text", "") or "")
    soup = BeautifulSoup(html_text, "html.parser") if html_text else None
    candidates: list[str] = []

    if soup:
        for anchor in soup.find_all("a", href=True):
            href = unquote(str(anchor.get("href", "")))
            if href.casefold().startswith("mailto:"):
                raw = href[7:].split("?", 1)[0]
                candidates.extend(EMAIL_RE.findall(raw))
        for tag in soup.select("[data-cfemail]"):
            decoded = decode_cfemail(str(tag.get("data-cfemail", "")))
            if decoded:
                candidates.append(decoded)

    footer_text = str(page.get("footer_text", "") or "")
    searchable = _deobfuscate_text(visible_text + "\n" + footer_text + "\n" + html_text)
    candidates.extend(EMAIL_RE.findall(searchable))
    out: list[EmailEvidence] = []
    seen: set[str] = set()
    for candidate in candidates:
        email = _normalize_email(candidate)
        if not _is_valid_email(email) or email in seen:
            continue
        seen.add(email)
        out.append(EmailEvidence(email, url, status))
    return out


def name_email_variants(full_name: str) -> set[str]:
    """Generate documented local-part forms for a person's grounded name."""
    words = _normalized_name_words(full_name)
    if len(words) < 2:
        return set()
    first, last = words[0], words[-1]
    raw = {
        first,
        first + last,
        first + "." + last,
        first + "_" + last,
        first + "-" + last,
        first[0] + last,
        first[0] + "." + last,
        last + "." + first,
        last + first,
    }
    if len(words) > 2:
        middle = "".join(words[1:-1])
        initials = "".join(word[0] for word in words[:-1])
        raw.update({first + middle + last, initials + last, first + "." + middle + "." + last})
    return {re.sub(r"[^a-z0-9]", "", value.casefold()) for value in raw if value}


def email_matches_person(email: str, full_name: str) -> bool:
    if "@" not in email:
        return False
    local = _normalize_email(email).split("@", 1)[0].split("+", 1)[0]
    normalized_local = re.sub(r"[^a-z0-9]", "", _ascii(local).casefold())
    return bool(normalized_local and normalized_local in name_email_variants(full_name))


def _default_mx_lookup(domain: str) -> bool:
    """Check MX records using dnspython; OFFLINE mode never performs DNS."""
    if os.getenv("OFFLINE", "0") == "1":
        return False
    try:
        import dns.resolver
        answer = list(dns.resolver.resolve(domain, "MX", lifetime=4.0))
        # RFC 7505 Null MX (a single target of '.') explicitly means no mail.
        if len(answer) == 1 and str(answer[0].exchange) == ".":
            return False
        return bool(answer) and any(str(record.exchange) != "." for record in answer)
    except Exception as exc:
        logger.info("MX lookup failed for %s: %s", domain, type(exc).__name__)
        return False


def _profile_link_is_candidate(url: str, org_domain: str, person: Person) -> bool:
    try:
        parsed = urlparse(url)
        host_value = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host_value:
        return False
    host = host_value.casefold()
    path_parts = [part.casefold() for part in parsed.path.split("/") if part]
    if not path_parts:
        return False
    if any(host == domain or host.endswith("." + domain) for domain in PROFILE_HOSTS):
        if host.endswith("linkedin.com"):
            return len(path_parts) >= 2 and path_parts[0] == "in"
        return path_parts[0] not in PROFILE_PATH_BLOCKLIST and path_parts[0] not in {"p", "reel"}
    clean_host = host[4:] if host.startswith("www.") else host
    if clean_host == org_domain or clean_host.endswith("." + org_domain):
        path = "/".join(path_parts)
        person_slug = "".join(_normalized_name_words(person.full_name))
        normalized_path = re.sub(r"[^a-z0-9]", "", path)
        page_terms = {"team", "people", "person", "leadership", "staff", "faculty", "committee", "member", "profile"}
        return any(term in path_parts for term in page_terms) or bool(person_slug and person_slug in normalized_path)
    return False


def _linked_profile_urls(org: Any, person: Person, pages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in pages:
        if str(page.get("status", "")).casefold() != "ok":
            continue
        source_url = str(page.get("url", "") or "")
        if not source_url or not page.get("html"):
            continue
        soup = BeautifulSoup(str(page["html"]), "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            target = urljoin(source_url, href)
            if not _profile_link_is_candidate(target, org.domain, person) or target in seen:
                continue
            seen.add(target)
            links.append((target, source_url))
    return links


def _default_profile_fetch(url: str) -> dict[str, Any] | None:
    """Make one plain HTTP request; no login, scraping workaround, or retries."""
    if os.getenv("OFFLINE", "0") == "1":
        return None
    try:
        import httpx
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=6.0,
        )
        return {
            "status_code": response.status_code,
            "final_url": str(response.url),
            "html": response.text if response.status_code == 200 else "",
        }
    except Exception as exc:
        logger.info("Profile-link request failed for %s: %s", url, type(exc).__name__)
        return None


def _profile_match_kind(page_html: str, person: Person, org_name: str) -> str | None:
    soup = BeautifulSoup(page_html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = " ".join(soup.get_text(" ", strip=True).split())[:12000]
    person_phrase = " ".join(_normalized_name_words(person.full_name))
    org_phrase = " ".join(_normalized_name_words(org_name))
    for candidate in (title, text):
        normalized = _ascii(candidate).casefold()
        if person_phrase and fuzz.partial_ratio(person_phrase, normalized) >= 88:
            return "person_name_match"
    for candidate in (title, text):
        normalized = _ascii(candidate).casefold()
        if org_phrase and fuzz.partial_ratio(org_phrase, normalized) >= 88:
            return "organization_name_match"
    return None


def _find_verified_profile(
    org: Any,
    person: Person,
    pages: list[dict[str, Any]],
    profile_fetch_fn: Callable[[str], Any],
) -> Contact | None:
    live_page_by_url = {
        str(page.get("url", "")): page
        for page in pages
        if str(page.get("status", "")).casefold() == "ok" and page.get("url")
    }
    for link_url, evidence_url in _linked_profile_urls(org, person, pages):
        target_page = live_page_by_url.get(link_url)
        if target_page is not None:
            response = {"status_code": 200, "html": target_page.get("html", "")}
        else:
            try:
                response = profile_fetch_fn(link_url)
            except Exception as exc:
                logger.info("Profile validation failed for %s: %s", link_url, type(exc).__name__)
                response = None
        if not response:
            continue
        if hasattr(response, "status_code"):
            response = {
                "status_code": response.status_code,
                "final_url": str(getattr(response, "url", "") or ""),
                "html": getattr(response, "text", "") if response.status_code == 200 else "",
            }
        if not isinstance(response, dict) or response.get("status_code") != 200:
            continue
        final_url = str(response.get("final_url", "") or "")
        if final_url and not _profile_link_is_candidate(final_url, org.domain, person):
            continue
        match_kind = _profile_match_kind(str(response.get("html", "")), person, org.org_name)
        if not match_kind:
            continue
        return Contact(
            channel=link_url,
            channel_type="profile_link",
            status="Verified",
            evidence_url=evidence_url,
            supporting_urls=[evidence_url, link_url],
            checks_passed=["linked_from_live_official_page", "http_200", match_kind],
        )
    return None


def _verified_company_level_contact(
    org: OrgCandidate,
    pages: list[dict[str, Any]],
    generic_inboxes: set[str],
    mx_lookup: Callable[[str], Any],
    mx_cache: dict[str, bool],
) -> VerifiedLead | None:
    """Verify a company contact published on its live official site."""
    candidates: list[EmailEvidence] = []
    for page in pages:
        if str(page.get("status", "")).casefold() != "ok" or not _page_is_official_domain(page, org.domain):
            continue
        for item in extract_page_emails(page):
            email_domain = _email_domain(item.address)
            same_org_domain = _same_org_domain(email_domain, org.domain)
            public_provider = _is_public_email_provider(email_domain)
            if not (same_org_domain or public_provider):
                continue
            # Company-domain addresses must be shared/role inboxes. Public
            # provider addresses are allowed because the official page itself
            # publishes them; their output label remains company-level unless
            # the exact address was grounded against a Step 3 person below.
            if public_provider or (same_org_domain and is_generic_inbox(item.address, generic_inboxes)):
                candidates.append(item)
    priority = {
        "partnership": 0, "partnerships": 0, "sponsor": 1, "sponsorship": 1,
        "business": 2, "corporate": 2, "outreach": 3, "connect": 3,
        "sales": 4, "contact": 5, "info": 6,
    }
    candidates.sort(key=lambda item: (
        "contact" not in item.source_url.casefold(),
        priority.get(_normalize_email(item.address).split("@", 1)[0].split("+", 1)[0], 10),
        item.source_url,
        item.address,
    ))
    for item in candidates:
        domain = _email_domain(item.address)
        if not _mx_passes(domain, mx_lookup, mx_cache):
            continue
        is_shared_inbox = is_generic_inbox(item.address, generic_inboxes)
        person = Person(
            full_name="General Contact" if is_shared_inbox else "Published Contact",
            role="Company Contact",
            source_url=item.source_url,
            role_score=0.0,
        )
        domain_check = (
            "same_org_domain"
            if _same_org_domain(domain, org.domain)
            else "published_public_email_provider"
        )
        contact = Contact(
            channel=item.address,
            channel_type="email",
            status="Verified",
            evidence_url=item.source_url,
            supporting_urls=[item.source_url],
            checks_passed=[
                "published_on_live_official_page",
                "company_level_shared_inbox" if is_shared_inbox else "officially_published_public_provider_contact",
                domain_check,
                "syntax_valid",
                "mx_ok",
            ],
        )
        return VerifiedLead(org=org, person=person, contact=contact)
    return None


def _personal_local_pattern(address: str) -> str | None:
    local = _normalize_email(address).split("@", 1)[0].split("+", 1)[0].casefold()
    if is_generic_inbox(address):
        return None
    if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}", local):
        separator = next(separator for separator in (".", "_", "-") if separator in local)
        return {".": "first.last", "_": "first_last", "-": "first-last"}[separator]
    if re.fullmatch(r"[a-z][._-][a-z]{2,}", local):
        separator = next(separator for separator in (".", "_", "-") if separator in local)
        return {".": "first_initial.last", "_": "first_initial_last", "-": "first_initial-last"}[separator]
    return None


def _infer_dominant_pattern(
    evidence: list[EmailEvidence],
    person: Person,
    org_domain: str,
    generic_inboxes: set[str],
) -> tuple[str, list[EmailEvidence]] | None:
    by_address: dict[str, EmailEvidence] = {}
    for item in evidence:
        if item.page_status != "ok" or _email_domain(item.address) != org_domain.casefold().strip("."):
            continue
        if is_generic_inbox(item.address, generic_inboxes) or email_matches_person(item.address, person.full_name):
            continue
        if not _is_valid_email(item.address):
            continue
        by_address.setdefault(item.address.casefold(), item)
    patterns = [(item, _personal_local_pattern(item.address)) for item in by_address.values()]
    patterns = [(item, pattern) for item, pattern in patterns if pattern]
    if len(patterns) < 2:
        return None
    counts = Counter(pattern for _item, pattern in patterns)
    pattern, count = counts.most_common(1)[0]
    if count < 2 or count / len(patterns) < 0.7:
        return None
    examples = [item for item, observed_pattern in patterns if observed_pattern == pattern]
    return pattern, examples


def _build_pattern_email(pattern: str, full_name: str, domain: str) -> str | None:
    words = _normalized_name_words(full_name)
    if len(words) < 2:
        return None
    first, last = words[0], words[-1]
    builders = {
        "first.last": f"{first}.{last}",
        "first_last": f"{first}_{last}",
        "first-last": f"{first}-{last}",
        "first_initial.last": f"{first[0]}.{last}",
        "first_initial_last": f"{first[0]}_{last}",
        "first_initial-last": f"{first[0]}-{last}",
    }
    local = builders.get(pattern)
    return f"{local}@{domain}" if local else None


def _mx_passes(domain: str, mx_lookup: Callable[[str], Any], cache: dict[str, bool]) -> bool:
    if domain not in cache:
        try:
            cache[domain] = bool(mx_lookup(domain))
        except Exception as exc:
            logger.info("MX verifier raised for %s: %s", domain, type(exc).__name__)
            cache[domain] = False
    return cache[domain]


def _pages_for_org(records: list[dict[str, Any]]) -> dict[str, tuple[Any, list[dict[str, Any]]]]:
    out: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
    for record in records:
        org = record.get("org")
        if org is None:
            continue
        domain = getattr(org, "domain", None) or (org.get("domain") if isinstance(org, dict) else None)
        if domain:
            prior = out.get(str(domain))
            pages = list(prior[1]) if prior else []
            pages.extend(record.get("pages") or [])
            out[str(domain)] = (org, pages)
    return out


def verify_contacts(
    step3_result: Step3Outcome | list[OrgWithPerson],
    discovery_records: list[dict[str, Any]],
    *,
    mx_lookup: Callable[[str], Any] | None = None,
    profile_fetch_fn: Callable[[str], Any] | None = None,
    generic_inboxes_path: Path = GENERIC_INBOXES_PATH,
) -> Step4Outcome:
    """Return only contacts that pass deterministic source and validation checks.

    Production MX/profile checks use simple network requests unless ``OFFLINE=1``.
    Unit tests should inject these callables; the test suite never performs DNS
    queries or makes HTTP requests.
    """
    identified = getattr(step3_result, "identified", step3_result)
    mx_lookup = mx_lookup or _default_mx_lookup
    profile_fetch_fn = profile_fetch_fn or _default_profile_fetch
    generic_inboxes = _load_generic_inboxes(generic_inboxes_path)
    org_pages = _pages_for_org(discovery_records)
    mx_cache: dict[str, bool] = {}
    outcome = Step4Outcome()

    # Keep every email visibly published on a live official page for review,
    # even when no named person was found. These are observations, not verified
    # person contacts; only the stricter checks below populate `verified`.
    observed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for domain, (org, pages) in org_pages.items():
        for page in pages:
            if str(page.get("status", "")).casefold() != "ok" or not _page_is_official_domain(page, domain):
                continue
            for item in extract_page_emails(page):
                key = (domain.casefold(), item.address.casefold())
                generic = is_generic_inbox(item.address, generic_inboxes)
                contact_row = observed_by_key.setdefault(key, {
                    "organization_name": str(org.get("org_name", "") if isinstance(org, dict) else getattr(org, "org_name", "")),
                    "root_domain": domain,
                    "channel": item.address,
                    "channel_type": "email",
                    "verification_status": "Observed—generic inbox; not person-verified" if generic else "Observed—person match not established",
                    "evidence_urls": [],
                    "checks_passed": ["published_on_live_official_domain_page"],
                })
                if item.source_url and item.source_url not in contact_row["evidence_urls"]:
                    contact_row["evidence_urls"].append(item.source_url)
    outcome.observed_contacts.extend(observed_by_key.values())

    for lead in identified:
        org = lead.org
        person = lead.person
        entry = org_pages.get(org.domain)
        if entry is None:
            outcome.dropped.append({
                "org_name": org.org_name,
                "domain": org.domain,
                "reason": "Step 2 source pages were not available for this organization",
            })
            continue
        _stored_org, pages = entry
        live_pages = [
            page for page in pages
            if str(page.get("status", "")).casefold() == "ok"
            and _page_is_official_domain(page, org.domain)
        ]
        evidence = [item for page in live_pages for item in extract_page_emails(page)]
        selected_contact: Contact | None = None

        exact_matches = []
        for item in evidence:
            if is_generic_inbox(item.address, generic_inboxes):
                continue
            email_domain = _email_domain(item.address)
            same_org_domain = _same_org_domain(email_domain, org.domain)
            public_provider = _is_public_email_provider(email_domain)
            if not (same_org_domain or public_provider):
                continue
            if not email_matches_person(item.address, person.full_name):
                continue
            exact_matches.append(item)
        exact_matches.sort(key=lambda item: ("contact" not in item.source_url.casefold(), item.source_url, item.address))
        for item in exact_matches:
            domain = _email_domain(item.address)
            if not _mx_passes(domain, mx_lookup, mx_cache):
                continue
            domain_check = "same_org_domain" if _same_org_domain(domain, org.domain) else "public_email_provider"
            selected_contact = Contact(
                channel=item.address,
                channel_type="email",
                status="Verified",
                evidence_url=item.source_url,
                supporting_urls=[item.source_url],
                checks_passed=[
                    "found_on_live_official_page",
                    "person_name_match",
                    domain_check,
                    "syntax_valid",
                    "mx_ok",
                ],
            )
            break

        if selected_contact is None and not exact_matches:
            pattern_result = _infer_dominant_pattern(evidence, person, org.domain, generic_inboxes)
            if pattern_result:
                pattern, examples = pattern_result
                candidate = _build_pattern_email(pattern, person.full_name, org.domain)
                if candidate and _is_valid_email(candidate) and not is_generic_inbox(candidate, generic_inboxes):
                    domain = _email_domain(candidate)
                    if _mx_passes(domain, mx_lookup, mx_cache):
                        selected_contact = Contact(
                            channel=candidate,
                            channel_type="email",
                            status="Pattern-Derived",
                            evidence_url=examples[0].source_url,
                            supporting_urls=list(dict.fromkeys(item.source_url for item in examples)),
                            checks_passed=[
                                "pattern_observed_on_same_domain",
                                f"dominant_pattern:{pattern}",
                                f"pattern_examples:{len(examples)}",
                                "constructed_from_grounded_person_name",
                                "syntax_valid",
                                "mx_ok",
                            ],
                        )

        if selected_contact is None:
            selected_contact = _find_verified_profile(
                org,
                person,
                live_pages,
                profile_fetch_fn,
            )

        if selected_contact:
            outcome.verified.append(VerifiedLead(org=org, person=person, contact=selected_contact))
        else:
            if not live_pages:
                reason = "no live official-domain pages available for contact extraction"
            elif exact_matches and not any(_mx_passes(_email_domain(item.address), mx_lookup, mx_cache) for item in exact_matches):
                reason = "person-matched email found but its domain has no verified MX record"
            else:
                reason = "no verifiable person-specific email or matching profile link"
            outcome.dropped.append({"org_name": org.org_name, "domain": org.domain, "reason": reason})

    # If no person-specific channel passed, a live official shared inbox may
    # still be a valid organization-level route. Keep its identity explicit;
    # never label it as a named decision-maker.
    verified_domains = {str(item.org.domain).casefold() for item in outcome.verified}
    for domain, (stored_org, pages) in org_pages.items():
        if domain.casefold() in verified_domains:
            continue
        org = stored_org if isinstance(stored_org, OrgCandidate) else OrgCandidate.model_validate(stored_org)
        general_contact = _verified_company_level_contact(
            org, pages, generic_inboxes, mx_lookup, mx_cache
        )
        if general_contact is not None:
            outcome.verified.append(general_contact)
            verified_domains.add(domain.casefold())

    # A general contact supersedes a person-specific-contact failure for the
    # same organization; leave unrelated Step 4 drop reasons intact.
    outcome.dropped = [
        item for item in outcome.dropped
        if str(item.get("domain", "")).casefold() not in verified_domains
    ]
    return outcome


def _page_is_official_domain(page: dict[str, Any], org_domain: str) -> bool:
    url = str(page.get("url", "") or "")
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host == org_domain or host.endswith("." + org_domain)


__all__ = [
    "Contact",
    "VerifiedLead",
    "EmailEvidence",
    "Step4Outcome",
    "decode_cfemail",
    "extract_page_emails",
    "is_generic_inbox",
    "name_email_variants",
    "email_matches_person",
    "verify_contacts",
]
