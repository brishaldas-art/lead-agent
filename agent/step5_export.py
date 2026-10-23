"""Step 5: validate, deduplicate, persist locally, and optionally export leads.

CSV, SQLite, and a local audit CSV are always written first. Google Sheets is
used only when both SHEET_ID and GOOGLE_CREDENTIALS are configured; credentials
are loaded lazily and are never required for offline/local export tests.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rapidfuzz import fuzz

from agent.step4_contacts import Step4Outcome, VerifiedLead
from agent.location import city_pattern
from agent.page_content import extract_footer_text

logger = logging.getLogger("agent.step5")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "outputs"
MAIN_HEADERS = (
    "Organization Name",
    "Category",
    "Location",
    "Contact Person",
    "Contact Channel",
    "Verification Status",
)
LEAD_ALL_HEADERS = (
    *MAIN_HEADERS,
    "Why Not Verified",
)
AUDIT_HEADERS = (
    "Organization Name",
    "Root Domain",
    "Contact Person",
    "Role",
    "Contact Channel",
    "Channel Type",
    "Verification Status",
    "Person Evidence URL",
    "Contact Evidence URL",
    "Supporting URLs",
    "Location Evidence URLs",
    "Checks Passed",
    "Normalized Contact Key",
)


class SheetRow(BaseModel):
    """The exact six-column public output schema."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_name: str = Field(min_length=2)
    category: Literal["Sponsor", "College Delegation"]
    location: str = Field(min_length=1)
    contact_person: str = Field(min_length=2)
    contact_channel: str = Field(min_length=3)
    verification_status: Literal["Verified", "Pattern-Derived"]

    def values(self) -> list[str]:
        data = self.model_dump()
        return [data[field] for field in (
            "organization_name", "category", "location", "contact_person",
            "contact_channel", "verification_status",
        )]


@dataclass(frozen=True)
class PreparedLead:
    row: SheetRow
    root_domain: str
    channel_key: str
    channel_type: str
    person_evidence_url: str
    contact_evidence_url: str
    supporting_urls: list[str]
    location_evidence_urls: list[str]
    checks_passed: list[str]

    def audit_values(self) -> list[str]:
        return [
            self.row.organization_name,
            self.root_domain,
            self.row.contact_person.split(",", 1)[0].strip(),
            self.row.contact_person.split(",", 1)[1].strip() if "," in self.row.contact_person else "",
            self.row.contact_channel,
            self.channel_type,
            self.row.verification_status,
            self.person_evidence_url,
            self.contact_evidence_url,
            json.dumps(self.supporting_urls, ensure_ascii=False),
            json.dumps(self.location_evidence_urls, ensure_ascii=False),
            json.dumps(self.checks_passed, ensure_ascii=False),
            self.channel_key,
        ]


@dataclass
class Step5Outcome:
    rows_added: int = 0
    total_rows: int = 0
    duplicates_skipped: int = 0
    dropped: list[dict[str, str]] = field(default_factory=list)
    csv_path: str = ""
    audit_csv_path: str = ""
    lead_all_csv_path: str = ""
    database_path: str = ""
    review_candidates: int = 0
    run_id: int = 0
    sheets_status: str = "not_configured"
    sheets_message: str = ""
    sheet_url: str = ""


@dataclass(frozen=True)
class _StoredLead:
    row: SheetRow
    root_domain: str
    channel_key: str
    person_evidence_url: str
    contact_evidence_url: str
    supporting_urls: list[str]
    location_evidence_urls: list[str]
    checks_passed: list[str]
    channel_type: str

    def audit_values(self) -> list[str]:
        prepared = PreparedLead(
            row=self.row,
            root_domain=self.root_domain,
            channel_key=self.channel_key,
            channel_type=self.channel_type,
            person_evidence_url=self.person_evidence_url,
            contact_evidence_url=self.contact_evidence_url,
            supporting_urls=self.supporting_urls,
            location_evidence_urls=self.location_evidence_urls,
            checks_passed=self.checks_passed,
        )
        return prepared.audit_values()


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _root_domain(value: str) -> str:
    domain = _clean_text(value).casefold().strip(".")
    return domain[4:] if domain.startswith("www.") else domain


def _normalize_channel(channel: str, channel_type: str) -> str:
    value = _clean_text(channel)
    if channel_type == "email":
        return value.casefold()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = f"{host}:{port}" if port and port not in {80, 443} else host
    path = re.sub(r"/+$", "", parsed.path) or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))


def _official_live_page(page: dict[str, Any], root_domain: str) -> bool:
    if str(page.get("status", "")).casefold() != "ok":
        return False
    try:
        host = (urlsplit(str(page.get("url", ""))).hostname or "").casefold()
    except ValueError:
        return False
    host = host[4:] if host.startswith("www.") else host
    return bool(root_domain and (host == root_domain or host.endswith("." + root_domain)))


def _page_verification_text(page: dict[str, Any]) -> str:
    """Combine extracted main text with visible HTML text for strict evidence checks.

    Main-text extractors often omit navigation or footers. Official location/contact
    details can legitimately appear there, so inspect the fetched HTML as well,
    while discarding non-visible script/style/template content.
    """
    parts = [
        _clean_text(page.get("text", "")),
        _clean_text(page.get("rendered_text", "")),
        _clean_text(page.get("footer_text", "")),
    ]
    raw_html = str(page.get("html", "") or "")
    rendered_html = str(page.get("rendered_html", "") or "")
    for footer_html in (raw_html, rendered_html):
        marked_footer_text = extract_footer_text(footer_html)
        if marked_footer_text:
            parts.append(marked_footer_text)
    if raw_html:
        try:
            soup = BeautifulSoup(raw_html, "html.parser")
            for node in soup(["script", "style", "noscript", "template"]):
                node.decompose()
            visible_text = _clean_text(soup.get_text(" "))
            if visible_text:
                parts.append(visible_text)
        except Exception:
            pass
    return _clean_text(" ".join(part for part in parts if part))


def _location_evidence(lead: VerifiedLead, records_by_domain: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    domain = _root_domain(lead.org.domain)
    entry = records_by_domain.get(domain, {})
    pages = entry.get("pages", [])
    # Prefer the organization's classified city, but only retain it when a live
    # official-domain page independently confirms it. Search city is a fallback
    # candidate only; it is subject to the same page-text check.
    city = _clean_text(lead.org.city) or _clean_text(entry.get("target_city", ""))
    if not city:
        return "", []
    city_matcher = city_pattern(city)
    sources: list[str] = []
    for page in pages:
        if not _official_live_page(page, domain):
            continue
        text = _page_verification_text(page)
        if city_matcher and city_matcher.search(text):
            url = _clean_text(page.get("url", ""))
            if url and url not in sources:
                sources.append(url)
    return (city, sources) if sources else ("", [])


def _prepare_lead(
    lead: VerifiedLead,
    records_by_domain: dict[str, dict[str, Any]],
) -> PreparedLead:
    org = lead.org
    person = lead.person
    contact = lead.contact
    if not bool(_value(org, "relevance_verified", True)):
        raise ValueError("organization relevance is awaiting human review")
    city, location_urls = _location_evidence(lead, records_by_domain)
    if not city:
        raise ValueError("organization city is not confirmed in live official-domain page text")
    channel_type = str(contact.channel_type)
    channel = _clean_text(contact.channel)
    if channel_type != "email":
        raise ValueError("only published email addresses qualify as contact channels")
    if "@" not in channel:
        raise ValueError("email contact channel is malformed")

    person_name = _clean_text(person.full_name)
    person_role = _clean_text(person.role)
    if not person_name or not person_role:
        raise ValueError("contact person name and role must both be present")
    row = SheetRow(
        organization_name=_clean_text(org.org_name),
        category=org.category,
        location=city,
        contact_person=f"{person_name}, {person_role}",
        contact_channel=channel,
        verification_status=contact.status,
    )
    if any(value.lstrip().startswith(("=", "+", "-", "@")) for value in row.values()):
        raise ValueError("spreadsheet formula-like values are not exportable")
    supporting = list(dict.fromkeys(
        [str(url) for url in contact.supporting_urls] + [str(contact.evidence_url)]
    ))
    return PreparedLead(
        row=row,
        root_domain=_root_domain(org.domain),
        channel_key=_normalize_channel(channel, channel_type),
        channel_type=channel_type,
        person_evidence_url=str(person.source_url),
        contact_evidence_url=str(contact.evidence_url),
        supporting_urls=supporting,
        location_evidence_urls=location_urls,
        checks_passed=list(contact.checks_passed),
    )


def _is_duplicate(candidate: PreparedLead, prior: PreparedLead | _StoredLead) -> bool:
    if candidate.root_domain == prior.root_domain and candidate.channel_key == prior.channel_key:
        return True
    same_channel = candidate.channel_key == prior.channel_key
    similar_name = fuzz.token_sort_ratio(
        candidate.row.organization_name.casefold(), prior.row.organization_name.casefold()
    ) >= 92
    return same_channel and similar_name


def _write_csv(path: Path, rows: list[_StoredLead]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(MAIN_HEADERS)
        writer.writerows(item.row.values() for item in rows)
    temporary.replace(path)


def _write_audit_csv(path: Path, rows: list[_StoredLead]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(AUDIT_HEADERS)
        writer.writerows(item.audit_values() for item in rows)
    temporary.replace(path)


def _csv_safe(value: Any) -> str:
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _json_list(value: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _why_not_verified(reasons: list[Any], *, is_exported: bool, observed_status: str = "") -> str:
    if is_exported:
        return ""
    # Sponsorship evidence is review context, not a Step 5 acceptance gate.
    non_gating_phrases = (
        "direct sponsorship is not confirmed",
        "no direct sponsorship evidence",
    )
    filtered = [
        _clean_text(reason) for reason in reasons
        if _clean_text(reason)
        and not any(phrase in _clean_text(reason).casefold() for phrase in non_gating_phrases)
    ]
    if observed_status and not any("observed" in reason.casefold() for reason in filtered):
        filtered.append(observed_status)
    if not filtered:
        filtered.append("No verified contact or qualifying export evidence was available.")
    return "; ".join(dict.fromkeys(filtered))


def _write_lead_all_csv(database_path: Path, path: Path) -> None:
    columns = (
        "organization_name", "category", "target_location", "root_domain",
        "step3_people_json", "observed_contacts_json", "step4_contacts_json",
        "review_reasons_json",
    )
    with closing(sqlite3.connect(database_path)) as connection:
        _create_tables(connection)
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM candidate_review ORDER BY organization_name COLLATE NOCASE"
        ).fetchall()
        accepted_keys = {
            (str(domain), str(channel_key))
            for domain, channel_key in connection.execute(
                "SELECT root_domain, normalized_contact_key FROM leads"
            ).fetchall()
        }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(LEAD_ALL_HEADERS)
        # Keep one compact row per organization; the SQLite review table retains
        # the full source, evidence, and audit detail behind this summary.
        for row in rows:
            org_name, category, location, domain, people_json, observed_json, contacts_json, reasons_json = row
            people = _json_list(people_json)
            observed = _json_list(observed_json)
            contacts = _json_list(contacts_json)
            reasons = _json_list(reasons_json)
            contact = contacts[0] if contacts else {}
            observed_contact = observed[0] if observed else {}
            observed_status = _clean_text(observed_contact.get("verification_status", ""))

            if contact:
                person_text = ", ".join(part for part in (
                    _clean_text(contact.get("person_name", "")),
                    _clean_text(contact.get("person_role", "")),
                ) if part)
                channel = _clean_text(contact.get("channel", ""))
                channel_type = _clean_text(contact.get("channel_type", ""))
                channel_key = _normalize_channel(channel, channel_type)
                is_exported = (str(domain), channel_key) in accepted_keys
                verification_status = (
                    _clean_text(contact.get("status", "Verified"))
                    if is_exported
                    else "Contact Verified; Lead Not Exported"
                )
            else:
                person_text = ""
                channel = ""
                channel_key = ""
                is_exported = False
                verification_status = "Not Verified"
                if observed_contact:
                    channel = _clean_text(observed_contact.get("channel", ""))
                    observed_status = observed_status or "Observed contact; not verified"
                if people:
                    person = people[0]
                    person_text = ", ".join(part for part in (
                        _clean_text(person.get("person_name", "")),
                        _clean_text(person.get("person_role", "")),
                    ) if part)
                elif observed_contact and "generic inbox" in observed_status.casefold():
                    person_text = "General Contact, Company Contact"
                elif observed_contact:
                    person_text = "Published Contact, Company Contact"

            why = _why_not_verified(
                reasons,
                is_exported=is_exported,
                observed_status=observed_status if not contact else "",
            )
            display_row = [
                org_name,
                category,
                location,
                person_text,
                channel,
                verification_status,
                why,
            ]
            writer.writerow([_csv_safe(value) for value in display_row])
    temporary.replace(path)


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS leads (
            organization_name TEXT NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('Sponsor', 'College Delegation')),
            location TEXT NOT NULL,
            contact_person TEXT NOT NULL,
            contact_channel TEXT NOT NULL,
            verification_status TEXT NOT NULL CHECK (verification_status IN ('Verified', 'Pattern-Derived')),
            root_domain TEXT NOT NULL,
            normalized_contact_key TEXT NOT NULL,
            PRIMARY KEY (root_domain, normalized_contact_key)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            root_domain TEXT NOT NULL,
            normalized_contact_key TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            person_evidence_url TEXT NOT NULL,
            contact_evidence_url TEXT NOT NULL,
            supporting_urls_json TEXT NOT NULL,
            location_evidence_urls_json TEXT NOT NULL,
            checks_passed_json TEXT NOT NULL,
            PRIMARY KEY (root_domain, normalized_contact_key),
            FOREIGN KEY (root_domain, normalized_contact_key)
                REFERENCES leads(root_domain, normalized_contact_key)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS candidate_review (
            root_domain TEXT PRIMARY KEY,
            organization_name TEXT NOT NULL,
            category TEXT NOT NULL,
            target_location TEXT NOT NULL,
            homepage TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            candidate_status TEXT NOT NULL DEFAULT '',
            classification_status TEXT NOT NULL DEFAULT 'verified',
            official_location_evidence_json TEXT NOT NULL,
            direct_sponsorship_evidence_json TEXT NOT NULL,
            related_csr_evidence_json TEXT NOT NULL,
            step3_people_json TEXT NOT NULL DEFAULT '[]',
            step4_contacts_json TEXT NOT NULL,
            observed_contacts_json TEXT NOT NULL DEFAULT '[]',
            discovery_only_evidence_json TEXT NOT NULL,
            pages_json TEXT NOT NULL,
            review_reasons_json TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at_utc TEXT NOT NULL,
            category TEXT NOT NULL,
            target_location TEXT NOT NULL,
            discovered_candidates INTEGER NOT NULL,
            verified_contacts INTEGER NOT NULL,
            local_leads_added INTEGER NOT NULL,
            total_local_leads INTEGER NOT NULL,
            duplicates_skipped INTEGER NOT NULL,
            invalid_rows_dropped INTEGER NOT NULL
        )"""
    )
    # Add review fields to existing local databases without discarding history.
    candidate_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(candidate_review)").fetchall()
    }
    for name, definition in (
        ("candidate_status", "TEXT NOT NULL DEFAULT ''"),
        ("classification_status", "TEXT NOT NULL DEFAULT 'verified'"),
        ("step3_people_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("observed_contacts_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in candidate_columns:
            connection.execute(f"ALTER TABLE candidate_review ADD COLUMN {name} {definition}")


def _remove_legacy_non_email_leads(connection: sqlite3.Connection) -> int:
    """Prune previously accepted profile links from the email-only lead store."""
    keys = connection.execute(
        """SELECT l.root_domain, l.normalized_contact_key
           FROM leads AS l LEFT JOIN audit_log AS a
           USING (root_domain, normalized_contact_key)
           WHERE COALESCE(a.channel_type, '') != 'email'
              OR l.contact_channel LIKE 'http://%'
              OR l.contact_channel LIKE 'https://%'"""
    ).fetchall()
    for root_domain, contact_key in keys:
        connection.execute(
            "DELETE FROM audit_log WHERE root_domain = ? AND normalized_contact_key = ?",
            (root_domain, contact_key),
        )
        connection.execute(
            "DELETE FROM leads WHERE root_domain = ? AND normalized_contact_key = ?",
            (root_domain, contact_key),
        )
    if keys:
        logger.info("Removed %d legacy non-email lead(s) from verified exports", len(keys))
    return len(keys)


def _read_stored(connection: sqlite3.Connection) -> list[_StoredLead]:
    rows = connection.execute(
        """SELECT l.organization_name, l.category, l.location, l.contact_person,
                  l.contact_channel, l.verification_status, l.root_domain,
                  l.normalized_contact_key, a.channel_type, a.person_evidence_url,
                  a.contact_evidence_url, a.supporting_urls_json,
                  a.location_evidence_urls_json, a.checks_passed_json
           FROM leads AS l LEFT JOIN audit_log AS a
           USING (root_domain, normalized_contact_key)
           ORDER BY l.rowid"""
    ).fetchall()
    result: list[_StoredLead] = []
    for values in rows:
        row = SheetRow(
            organization_name=values[0], category=values[1], location=values[2],
            contact_person=values[3], contact_channel=values[4], verification_status=values[5],
        )
        result.append(_StoredLead(
            row=row,
            root_domain=values[6],
            channel_key=values[7],
            channel_type=values[8] or "",
            person_evidence_url=values[9] or "",
            contact_evidence_url=values[10] or "",
            supporting_urls=json.loads(values[11] or "[]"),
            location_evidence_urls=json.loads(values[12] or "[]"),
            checks_passed=json.loads(values[13] or "[]"),
        ))
    return result


def _persist_local(
    candidates: list[PreparedLead], output_dir: Path
) -> tuple[list[_StoredLead], int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = output_dir / "leads.db"
    added = 0
    duplicates = 0
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _create_tables(connection)
        _remove_legacy_non_email_leads(connection)
        prior = _read_stored(connection)
        for candidate in candidates:
            if any(_is_duplicate(candidate, existing) for existing in prior):
                duplicates += 1
                continue
            connection.execute("SAVEPOINT add_lead")
            try:
                connection.execute(
                    """INSERT INTO leads (
                        organization_name, category, location, contact_person,
                        contact_channel, verification_status, root_domain,
                        normalized_contact_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*candidate.row.values(), candidate.root_domain, candidate.channel_key),
                )
                connection.execute(
                    """INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.root_domain,
                        candidate.channel_key,
                        candidate.channel_type,
                        candidate.person_evidence_url,
                        candidate.contact_evidence_url,
                        json.dumps(candidate.supporting_urls, ensure_ascii=False),
                        json.dumps(candidate.location_evidence_urls, ensure_ascii=False),
                        json.dumps(candidate.checks_passed, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK TO add_lead")
                connection.execute("RELEASE add_lead")
                duplicates += 1
                continue
            connection.execute("RELEASE add_lead")
            prior.append(_StoredLead(
                row=candidate.row,
                root_domain=candidate.root_domain,
                channel_key=candidate.channel_key,
                channel_type=candidate.channel_type,
                person_evidence_url=candidate.person_evidence_url,
                contact_evidence_url=candidate.contact_evidence_url,
                supporting_urls=candidate.supporting_urls,
                location_evidence_urls=candidate.location_evidence_urls,
                checks_passed=candidate.checks_passed,
            ))
            added += 1
        stored = _read_stored(connection)
    _write_csv(output_dir / "leads.csv", stored)
    _write_audit_csv(output_dir / "audit_log.csv", stored)
    return stored, added, duplicates


def _persist_candidate_review(
    database_path: Path,
    discovery_records: list[dict[str, Any]],
    step4_result: Any,
    *,
    people_result: Any = None,
    run_context: dict[str, Any],
    export_drops: list[dict[str, str]],
    leads_added: int,
    total_leads: int,
    duplicates_skipped: int,
    invalid_rows_dropped: int,
) -> tuple[int, int]:
    """Keep discovered candidates and a run ledger separate from verified leads."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verified_contacts = list(getattr(step4_result, "verified", step4_result) or [])
    observed_contacts = list(getattr(step4_result, "observed_contacts", []) or [])
    people_by_domain: dict[str, list[dict[str, Any]]] = {}
    for entry in getattr(people_result, "identified", []) or []:
        org = _value(entry, "org", {})
        domain = _root_domain(str(_value(org, "domain", "")))
        candidates = list(_value(entry, "all_candidates", []) or []) or [_value(entry, "person", {})]
        if domain:
            people_by_domain[domain] = [{
                "person_name": _clean_text(_value(person, "full_name", "")),
                "person_role": _clean_text(_value(person, "role", "")),
                "source_url": _clean_text(_value(person, "source_url", "")),
                "role_score": _value(person, "role_score", ""),
            } for person in candidates]
    people_drop_reasons: dict[str, list[str]] = {}
    for item in getattr(people_result, "dropped", []) or []:
        domain = _root_domain(str(item.get("domain", "")))
        if domain:
            people_drop_reasons.setdefault(domain, []).append(str(item.get("reason", "")))
    observed_by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in observed_contacts:
        domain = _root_domain(str(item.get("root_domain", "")))
        if domain:
            observed_by_domain.setdefault(domain, []).append(item)
    contact_domains = {
        _root_domain(str(_value(_value(item, "org", {}), "domain", "")))
        for item in verified_contacts
    }
    contacts_by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in verified_contacts:
        org = _value(item, "org", {})
        domain = _root_domain(str(_value(org, "domain", "")))
        contact = _value(item, "contact", {})
        person = _value(item, "person", {})
        if domain:
            contacts_by_domain.setdefault(domain, []).append({
                "person_name": _clean_text(_value(person, "full_name", "")),
                "person_role": _clean_text(_value(person, "role", "")),
                "channel": _clean_text(_value(contact, "channel", "")),
                "channel_type": _clean_text(_value(contact, "channel_type", "")),
                "status": _clean_text(_value(contact, "status", "")),
                "evidence_url": _clean_text(_value(contact, "evidence_url", "")),
                "supporting_urls": [str(url) for url in (_value(contact, "supporting_urls", []) or [])],
                "checks_passed": list(_value(contact, "checks_passed", []) or []),
            })
    contact_drop_reasons: dict[str, list[str]] = {}
    for item in getattr(step4_result, "dropped", []) or []:
        domain = _root_domain(str(item.get("domain", "")))
        if domain:
            contact_drop_reasons.setdefault(domain, []).append(str(item.get("reason", "")))
    category = _clean_text(run_context.get("category", ""))
    run_location = _clean_text(run_context.get("target_location", ""))
    seen_domains: set[str] = set()

    with closing(sqlite3.connect(database_path)) as connection, connection:
        _create_tables(connection)
        for record in discovery_records:
            org = record.get("org")
            if org is None:
                continue
            org_name = _clean_text(_value(org, "org_name", "unknown organization"))
            domain = _root_domain(str(_value(org, "domain", ""))) or f"unknown:{org_name.casefold()}"
            seen_domains.add(domain)
            verification = record.get("verification") or {}
            target_location = _clean_text(
                _value(verification, "target_city", "")
                or run_location
                or _value(org, "city", "")
            )
            official_location_urls: list[str] = []
            if target_location:
                location_pattern = city_pattern(target_location)
                for page in record.get("pages", []) or []:
                    if _official_live_page(page, domain) and location_pattern.search(_page_verification_text(page)):
                        page_url = _clean_text(page.get("url", ""))
                        if page_url and page_url not in official_location_urls:
                            official_location_urls.append(page_url)

            direct_urls = list(_value(verification, "direct_sponsorship_evidence_urls", []) or [])
            related_urls = list(_value(verification, "related_csr_community_evidence_urls", []) or [])
            reasons: list[str] = []
            if not official_location_urls:
                reasons.append("Target location is not confirmed in live official-page text.")
            if not direct_urls:
                reasons.append(
                    "Related CSR/community evidence only; direct sponsorship is not confirmed."
                    if related_urls
                    else "No direct sponsorship evidence was found in collected sources."
                )
            if domain not in contact_domains:
                reasons.extend(reason for reason in people_drop_reasons.get(domain, []) if reason)
                reasons.extend(reason for reason in contact_drop_reasons.get(domain, []) if reason)
                if not contact_drop_reasons.get(domain) and domain not in people_drop_reasons:
                    reasons.append("No verified contact was available for export in this run.")
            if record.get("candidate_status") not in (None, "official_page_available"):
                reasons.append(f"Candidate page status: {record.get('candidate_status')}.")
            classification_status = _clean_text(_value(org, "classification_status", "verified"))
            if classification_status != "gemini_grounded" and classification_status != "verified":
                reasons.append(f"Organization relevance needs review: {classification_status}.")
            for dropped in export_drops:
                if _clean_text(dropped.get("org_name", "")).casefold() == org_name.casefold():
                    reasons.append(str(dropped.get("reason", "")))

            pages_summary = [
                {
                    "url": _clean_text(page.get("url", "")),
                    "status": _clean_text(page.get("status", "")),
                    "http_status": page.get("http_status"),
                    "page_type": _clean_text(page.get("page_type", "")),
                    "footer_text_extracted": bool(_clean_text(page.get("footer_text", ""))),
                    "footer_text_excerpt": _clean_text(page.get("footer_text", ""))[:800],
                    "render_status": _clean_text(page.get("render_status", "")),
                    "render_url": _clean_text(page.get("render_url", "")),
                    "rendered_text_excerpt": _clean_text(page.get("rendered_text", ""))[:800],
                    "text_excerpt": _clean_text(page.get("text", ""))[:800],
                }
                for page in (record.get("pages", []) or [])
            ]
            discovery_only = [
                {
                    "evidence_type": _clean_text(item.get("evidence_type", "")),
                    "source": _clean_text(item.get("source", "")),
                    "title": _clean_text(item.get("title", "")),
                    "snippet": _clean_text(item.get("snippet", ""))[:500],
                }
                for item in (record.get("discovery_evidence", []) or [])
            ]
            first_seen = connection.execute(
                "SELECT first_seen_at_utc FROM candidate_review WHERE root_domain = ?",
                (domain,),
            ).fetchone()
            connection.execute(
                """INSERT INTO candidate_review (
                    root_domain, organization_name, category, target_location, homepage,
                    verification_status, candidate_status, classification_status, official_location_evidence_json,
                    direct_sponsorship_evidence_json, related_csr_evidence_json,
                    step3_people_json, step4_contacts_json, observed_contacts_json,
                    discovery_only_evidence_json, pages_json, review_reasons_json,
                    first_seen_at_utc, last_seen_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_domain) DO UPDATE SET
                    organization_name=excluded.organization_name,
                    category=excluded.category,
                    target_location=excluded.target_location,
                    homepage=excluded.homepage,
                    verification_status=excluded.verification_status,
                    candidate_status=excluded.candidate_status,
                    classification_status=excluded.classification_status,
                    official_location_evidence_json=excluded.official_location_evidence_json,
                    direct_sponsorship_evidence_json=excluded.direct_sponsorship_evidence_json,
                    related_csr_evidence_json=excluded.related_csr_evidence_json,
                    step3_people_json=excluded.step3_people_json,
                    step4_contacts_json=excluded.step4_contacts_json,
                    observed_contacts_json=excluded.observed_contacts_json,
                    discovery_only_evidence_json=excluded.discovery_only_evidence_json,
                    pages_json=excluded.pages_json,
                    review_reasons_json=excluded.review_reasons_json,
                    last_seen_at_utc=excluded.last_seen_at_utc""",
                (
                    domain,
                    org_name,
                    _clean_text(_value(org, "category", "")),
                    target_location,
                    _clean_text(_value(org, "homepage", "")),
                    _clean_text(_value(verification, "status", "not_checked")) or "not_checked",
                    _clean_text(record.get("candidate_status", "")),
                    _clean_text(_value(org, "classification_status", "verified")),
                    json.dumps(official_location_urls, ensure_ascii=False),
                    json.dumps(direct_urls, ensure_ascii=False),
                    json.dumps(related_urls, ensure_ascii=False),
                    json.dumps(people_by_domain.get(domain, []), ensure_ascii=False),
                    json.dumps(contacts_by_domain.get(domain, []), ensure_ascii=False),
                    json.dumps(observed_by_domain.get(domain, []), ensure_ascii=False),
                    json.dumps(discovery_only, ensure_ascii=False),
                    json.dumps(pages_summary, ensure_ascii=False),
                    json.dumps(list(dict.fromkeys(reason for reason in reasons if reason)), ensure_ascii=False),
                    first_seen[0] if first_seen else now,
                    now,
                ),
            )

        cursor = connection.execute(
            """INSERT INTO pipeline_runs (
                run_at_utc, category, target_location, discovered_candidates,
                verified_contacts, local_leads_added, total_local_leads,
                duplicates_skipped, invalid_rows_dropped
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now, category, run_location, len(seen_domains), len(verified_contacts),
                leads_added, total_leads, duplicates_skipped, invalid_rows_dropped,
            ),
        )
        run_id = int(cursor.lastrowid or 0)
    return len(seen_domains), run_id


def _ensure_worksheet(spreadsheet: Any, title: str, headers: tuple[str, ...]) -> tuple[Any, list[list[str]]]:
    worksheets = spreadsheet.worksheets()
    worksheet = next((item for item in worksheets if item.title.casefold() == title.casefold()), None)
    if worksheet is None and title == "leads" and worksheets:
        worksheet = worksheets[0]
    if worksheet is None:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
    existing = worksheet.get_all_values()
    if not existing:
        worksheet.update(values=[list(headers)], range_name="A1")
        existing = [list(headers)]
    elif tuple(existing[0][:len(headers)]) != headers or len(existing[0]) != len(headers):
        raise ValueError(f"worksheet {worksheet.title!r} has a header that does not match the required schema")
    return worksheet, existing


def _sheet_row_key(values: list[str]) -> tuple[str, str]:
    if len(values) < 5:
        return "", ""
    return (_clean_text(values[0]).casefold(), _normalize_channel(values[4], "profile_link" if values[4].startswith(("http://", "https://")) else "email"))


def _is_transient_sheet_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "code", None)
    return status in {429, 500, 502, 503, 504}


def _append_reconciled(worksheet: Any, rows: list[list[str]], key_fn: Callable[[list[str]], Any]) -> None:
    """Retry transient writes, rereading first to avoid duplicate appends."""
    if not rows:
        return
    last_error: Exception | None = None
    for attempt in range(3):
        current = worksheet.get_all_values()
        existing_keys = {key_fn(row) for row in current[1:] if row}
        pending = [row for row in rows if key_fn(row) not in existing_keys]
        if not pending:
            return
        try:
            worksheet.append_rows(pending, value_input_option="RAW")
            return
        except Exception as exc:
            last_error = exc
            if attempt == 2 or not _is_transient_sheet_error(exc):
                break
            time.sleep(0.5 * (2 ** attempt))
    assert last_error is not None
    raise last_error


def _sync_google_sheet(
    sheet_id: str,
    credentials_path: str,
    stored: list[_StoredLead],
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> str:
    if client_factory is None:
        try:
            import gspread
        except ImportError as exc:
            raise RuntimeError("gspread is not installed; run pip install -r requirements.txt") from exc
        client_factory = lambda filename: gspread.service_account(filename=filename)
    client = client_factory(credentials_path)
    spreadsheet = client.open_by_key(sheet_id)
    main_ws, main_values = _ensure_worksheet(spreadsheet, "leads", MAIN_HEADERS)
    audit_ws, audit_values = _ensure_worksheet(spreadsheet, "audit_log", AUDIT_HEADERS)

    # The six-column main sheet has no domain field. Use exact sheet row identity
    # plus fuzzy organization-name matching for legacy rows; the audit tab keeps
    # the authoritative root-domain/contact key for future synchronization.
    known_main = main_values[1:]
    main_to_add: list[list[str]] = []
    for item in stored:
        duplicate = any(
            _sheet_row_key(values)[1] == item.channel_key
            and fuzz.token_sort_ratio(_sheet_row_key(values)[0], item.row.organization_name.casefold()) >= 92
            for values in known_main
            if len(values) >= 6
        )
        if not duplicate:
            values = item.row.values()
            main_to_add.append(values)
            known_main.append(values)

    audit_keys = {
        (row[1], row[12]) for row in audit_values[1:] if len(row) >= 13
    }
    audit_to_add = [
        item.audit_values()
        for item in stored
        if (item.root_domain, item.channel_key) not in audit_keys
    ]
    _append_reconciled(main_ws, main_to_add, _sheet_row_key)
    _append_reconciled(
        audit_ws,
        audit_to_add,
        lambda row: (row[1], row[12]) if len(row) >= 13 else ("", ""),
    )
    return str(getattr(spreadsheet, "url", "") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")


def export_verified_leads(
    step4_result: Step4Outcome | list[VerifiedLead],
    discovery_records: list[dict[str, Any]],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    sheet_id: str | None = None,
    credentials_path: str | Path | None = None,
    sheets_client_factory: Callable[[str], Any] | None = None,
    run_context: dict[str, Any] | None = None,
    people_result: Any = None,
) -> Step5Outcome:
    """Export verified contacts. Local files are always persisted before Sheets.

    Omitting Sheet settings is the expected no-credentials mode and performs no
    network requests. Tests may inject a fake ``sheets_client_factory``; the
    production Google Sheets client is loaded only when both settings exist.
    """
    identified = getattr(step4_result, "verified", step4_result)
    records_by_domain: dict[str, dict[str, Any]] = {}
    for record in discovery_records:
        org = record.get("org")
        domain = _root_domain(str(_value(org, "domain", ""))) if org is not None else ""
        if domain:
            entry = records_by_domain.setdefault(domain, {"pages": [], "target_city": ""})
            entry["pages"].extend(record.get("pages") or [])
            verification = record.get("verification") or {}
            if not entry["target_city"]:
                entry["target_city"] = str(_value(verification, "target_city", "") or "")

    outcome = Step5Outcome()
    candidates: list[PreparedLead] = []
    for lead in identified or []:
        try:
            candidates.append(_prepare_lead(lead, records_by_domain))
        except (ValidationError, ValueError, TypeError) as exc:
            name = _clean_text(_value(_value(lead, "org", {}), "org_name", "unknown organization"))
            reason = str(exc) or type(exc).__name__
            outcome.dropped.append({"org_name": name, "reason": reason})
            logger.warning("Step 5 dropped %s: %s", name, reason)

    output_path = Path(output_dir)
    stored, added, duplicates = _persist_local(candidates, output_path)
    outcome.rows_added = added
    outcome.total_rows = len(stored)
    outcome.duplicates_skipped = duplicates
    outcome.csv_path = str(output_path / "leads.csv")
    outcome.audit_csv_path = str(output_path / "audit_log.csv")
    outcome.lead_all_csv_path = str(output_path / "lead_all.csv")
    outcome.database_path = str(output_path / "leads.db")
    outcome.review_candidates, outcome.run_id = _persist_candidate_review(
        Path(outcome.database_path),
        discovery_records,
        step4_result,
        people_result=people_result,
        run_context=run_context or {},
        export_drops=outcome.dropped,
        leads_added=outcome.rows_added,
        total_leads=outcome.total_rows,
        duplicates_skipped=outcome.duplicates_skipped,
        invalid_rows_dropped=len(outcome.dropped),
    )
    _write_lead_all_csv(Path(outcome.database_path), Path(outcome.lead_all_csv_path))

    configured_sheet_id = os.getenv("SHEET_ID", "") if sheet_id is None else str(sheet_id)
    configured_credentials = os.getenv("GOOGLE_CREDENTIALS", "") if credentials_path is None else str(credentials_path)
    configured_sheet_id = configured_sheet_id.strip()
    configured_credentials = configured_credentials.strip()
    if not configured_sheet_id and not configured_credentials:
        outcome.sheets_status = "not_configured"
        outcome.sheets_message = "Google Sheets skipped; lead_all.csv, leads.csv, audit CSV, and SQLite exports are ready."
        return outcome
    if not configured_sheet_id or not configured_credentials:
        outcome.sheets_status = "not_configured"
        outcome.sheets_message = "Google Sheets skipped; set both SHEET_ID and GOOGLE_CREDENTIALS to enable it."
        return outcome
    if not Path(configured_credentials).is_file():
        outcome.sheets_status = "failed"
        outcome.sheets_message = f"Credentials file not found: {configured_credentials}; local exports are intact."
        return outcome
    try:
        outcome.sheet_url = _sync_google_sheet(
            configured_sheet_id,
            configured_credentials,
            stored,
            client_factory=sheets_client_factory,
        )
        outcome.sheets_status = "synced"
        outcome.sheets_message = "Google Sheets synchronization completed."
    except Exception as exc:
        outcome.sheets_status = "failed"
        outcome.sheets_message = f"Google Sheets sync failed ({type(exc).__name__}); local exports are intact."
        logger.exception("Google Sheets synchronization failed")
    return outcome


__all__ = [
    "SheetRow",
    "Step5Outcome",
    "MAIN_HEADERS",
    "LEAD_ALL_HEADERS",
    "AUDIT_HEADERS",
    "export_verified_leads",
]
