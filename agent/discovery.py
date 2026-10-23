"""
Part 2 discovery loop: search, filter, classify, build orgs, fetch pages.
Dead pages fall back to search snippets. Re-planning is capped by REPLAN_ROUNDS.
No URL is invented: every URL comes from search results, a homepage, or a score_links href.
"""

import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import tldextract

from agent.search import run_query
from agent.filters import filter_results
from agent.classify import classify_results
from agent.orgs import build_orgs
from agent.crawl import score_links
from agent.fetch import fetch_page
from agent.page_content import extract_footer_text
from agent.replan import should_replan, propose_queries
from agent.evidence import (
    canonical_url_key,
    domain_matches_name,
    instagram_company_name,
    is_social_url,
    linkedin_company_name,
    listicle_names,
    result_matches_name,
    summarize_official_evidence,
)
from agent.location import text_mentions_city

logger = logging.getLogger("agent.discovery")

MIN_ORGS: int = int(os.getenv("MIN_ORGS", "5"))
MAX_ROUNDS: int = min(max(int(os.getenv("REPLAN_ROUNDS", "1")), 0), 1)
MAX_ORGS: int = int(os.getenv("MAX_ORGS", "20"))
MAX_CANDIDATES: int = int(os.getenv("MAX_CANDIDATES", "40"))
MAX_SEED_ORGS: int = int(os.getenv("MAX_SEED_ORGS", "10"))
MAX_ENRICHMENT_SEEDS: int = int(os.getenv("MAX_ENRICHMENT_SEEDS", "4"))
MAX_PAGES_PER_ORG: int = int(os.getenv("MAX_PAGES_PER_ORG", "3"))
TIME_BUDGET_MIN: float = float(os.getenv("TIME_BUDGET_MIN", "8"))
TRACE_PATH: Path = Path("outputs/agent_trace.log")
_RESULT_DOMAIN = tldextract.TLDExtract(suffix_list_urls=())

_trace_ready: bool = False


def setup_trace() -> None:
    """Attach one file handler to the agent logger tree, once per process."""
    global _trace_ready
    if _trace_ready:
        return
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(TRACE_PATH, mode="a", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    pipeline_logger = logging.getLogger("agent")
    pipeline_logger.addHandler(handler)
    pipeline_logger.setLevel(logging.INFO)
    _trace_ready = True


def _host(url: str) -> str:
    """Return the lowercase hostname without a leading 'www.'."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _page_type(url: str) -> str:
    low = url.lower()
    if "team" in low or "leadership" in low:
        return "team"
    if "about" in low:
        return "about"
    if "contact" in low:
        return "contact"
    if any(term in low for term in ("sponsor", "partner", "csr", "esg", "sustainability")):
        return "sponsorship"
    return "other"


def _safe_fetch(fetch_fn, url: str) -> dict:
    """Call fetch_fn and never raise. A failure is reported as a dead page."""
    try:
        result = fetch_fn(url)
        return result if isinstance(result, dict) else {"status": "dead"}
    except Exception as exc:
        logger.warning("fetch raised for %s: %s", url, exc)
        return {"status": "dead", "reason": str(exc)}


def _enrich_search_results(plan, primary_results: list[dict], search_fn, deadline: float | None = None):
    """Use social/list snippets only to seed bounded company-name searches.

    Social and listicle pages are returned separately as discovery evidence;
    only name-matched, brand-consistent non-social search results are candidates
    for classification and crawling.
    """
    if getattr(plan, "category", "") not in ("Sponsor", "Both"):
        return [], {}, []

    industries = getattr(plan, "industries", None) or []
    industry = str(industries[0]) if industries else "companies"
    city = str(getattr(plan, "city", None) or "India")
    social_queries = [
        f"site:linkedin.com/company {industry} {city}",
        f"site:instagram.com {industry} {city}",
    ]
    social_rows: list[dict] = []
    enrichment_queries: list[str] = []
    for query in social_queries:
        if deadline is not None and time.monotonic() >= deadline:
            break
        enrichment_queries.append(query)
        try:
            rows = search_fn(query) or []
        except Exception as exc:
            logger.warning("social discovery search failed for %r: %s", query, exc)
            continue
        social_rows.extend(row for row in rows if is_social_url(row.get("url", "")))

    _kept, dropped = filter_results(primary_results)
    seeds: list[tuple[str, str, dict]] = []

    def add_seed(name: str | None, source: str, evidence: dict) -> bool:
        if not name:
            return False
        if any(result_matches_name(name, {"title": existing, "snippet": ""}, threshold=85)
               for existing, _source, _evidence in seeds):
            return False
        seeds.append((name, source, evidence))
        return True

    linkedin_count = 0
    for row in social_rows:
        if linkedin_count >= 3:
            break
        name = linkedin_company_name(row)
        if add_seed(name, "linkedin_company_result", row):
            linkedin_count += 1

    instagram_count = 0
    for row in social_rows:
        if instagram_count >= 2:
            break
        name = instagram_company_name(row)
        if add_seed(name, "instagram_profile_or_post_result", row):
            instagram_count += 1

    listicle_count = 0
    for row in dropped:
        if row.get("drop_reason") != "listicle":
            continue
        for name in listicle_names(row.get("snippet", "")):
            if add_seed(name, "listicle_snippet", row):
                listicle_count += 1
            if listicle_count >= 2:
                break
        if listicle_count >= 2:
            break

    extra_results: list[dict] = []
    seed_evidence: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()
    for name, source, evidence in seeds[:MAX_ENRICHMENT_SEEDS]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        seed_evidence[name] = [{
            "evidence_type": "public_social_discovery_only" if source.startswith(("linkedin", "instagram")) else "listicle_discovery_only",
            "source": evidence.get("url", ""),
            "title": evidence.get("title", ""),
            "snippet": evidence.get("snippet", "") or "",
        }]
        query = f'"{name}" {city} official'
        enrichment_queries.append(query)
        try:
            rows = search_fn(query) or []
        except Exception as exc:
            logger.warning("name resolution search failed for %r: %s", query, exc)
            continue
        kept, _dropped = filter_results(rows)
        matches = [
            row for row in kept
            if result_matches_name(name, row)
            and domain_matches_name(name, row.get("url", ""))
        ]
        for row in matches[:1]:
            key = canonical_url_key(row.get("url", ""))
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            extra_results.append({
                **row,
                "_discovery_seed_name": name,
                "_discovery_seed_source": source,
            })

    logger.info(
        "name enrichment: social=%d seeds=%d verified_non_social_results=%d",
        len(social_rows), min(len(seeds), MAX_ENRICHMENT_SEEDS), len(extra_results),
    )
    return extra_results, seed_evidence, enrichment_queries


def _page_from_fetch(result: dict) -> dict:
    url = result.get("final_url") or ""
    raw_html = str(result.get("html", "") or "")
    footer_text = extract_footer_text(raw_html)
    if str(result.get("status", "")).casefold() == "ok":
        logger.info("footer inspected for %s: %d visible chars", url, len(footer_text))
    return {
        "url": url,
        "page_type": _page_type(url),
        "status": result.get("status", "dead"),
        "http_status": result.get("http_status"),
        "reason": result.get("reason", "") or "",
        "text": result.get("text", "") or "",
        "html": raw_html,
        "footer_text": footer_text,
    }


def _pages_from_fetch(result: dict) -> list[dict]:
    pages = [_page_from_fetch(result)]
    fallback = result.get("archive_fallback")
    if isinstance(fallback, dict) and fallback.get("final_url"):
        pages.append(_page_from_fetch(fallback))
    return pages


def _snippet_page(org, raw: list[dict], failed_home: dict | None = None) -> dict | None:
    """Use a same-domain search result as weak evidence after fetch failure."""
    for result in raw:
        url = result.get("url", "")
        host = _host(url)
        if host and (host == org.domain or host.endswith("." + org.domain)):
            text = f"{result.get('title', '')} {result.get('snippet', '')}".strip()
            return {
                "url": url,
                "page_type": "other",
                "status": "search_snippet",
                "http_status": (failed_home or {}).get("http_status"),
                "reason": (failed_home or {}).get("reason", "") or "",
                "text": text,
                "html": "",
            }
    return None


def _dedupe_results(rows: list[dict]) -> list[dict]:
    """Collapse repeated search hits before classification/model calls."""
    output: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url", ""))
        try:
            ext = _RESULT_DOMAIN(url)
            key = f"{ext.domain}.{ext.suffix}" if ext.domain and ext.suffix else canonical_url_key(url)
        except Exception:
            key = canonical_url_key(url)
        if not key:
            key = " ".join(str(row.get("title", "")).casefold().split())
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _resolve_seed_orgs(plan, search_fn, deadline: float) -> tuple[list, dict[str, list[dict]], list[str], list[dict]]:
    """Resolve brochure/prompt seed names to brand-consistent company domains."""
    category = getattr(plan, "category", "Sponsor")
    org_category = "College Delegation" if category == "College Delegation" else "Sponsor"
    city = str(getattr(plan, "city", None) or "")
    resolved = []
    evidence_by_name: dict[str, list[dict]] = {}
    used_queries: list[str] = []
    seed_rows: list[dict] = []
    seen_domains: set[str] = set()

    for raw_name in (getattr(plan, "seed_orgs", None) or [])[:MAX_SEED_ORGS]:
        if time.monotonic() >= deadline:
            break
        name = " ".join(str(raw_name).split())
        if len(name) < 2:
            continue
        query = f'"{name}" {city} official'.strip()
        used_queries.append(query)
        try:
            rows = search_fn(query) or []
        except Exception as exc:
            logger.warning("seed resolution failed for %r: %s", name, exc)
            continue
        kept, _dropped = filter_results(rows)
        matches = [
            row for row in kept
            if result_matches_name(name, row, threshold=78)
            and domain_matches_name(name, row.get("url", ""))
        ]
        if not matches:
            logger.info("seed unresolved to a brand-consistent domain: %s", name)
            continue
        row = matches[0]
        classified = [{
            "org_name": name,
            "category": org_category,
            "city": city if text_mentions_city(f"{row.get('title', '')} {row.get('snippet', '')}", city) else None,
            "url": row.get("url", ""),
            "title": row.get("title", ""),
            "snippet": row.get("snippet", ""),
        }]
        for org in build_orgs(classified, discovered_via="brochure_seed"):
            if org.domain in seen_domains:
                continue
            seen_domains.add(org.domain)
            resolved.append(org)
            seed_rows.append(row)
            evidence_by_name[name] = [{
                "evidence_type": "brochure_seed_domain_resolution",
                "source": str(row.get("url", "")),
                "title": str(row.get("title", "")),
                "snippet": str(row.get("snippet", ""))[:500],
            }]
    return resolved, evidence_by_name, used_queries, seed_rows


def _usable_official_record(record: dict) -> bool:
    return any(
        str(page.get("status", "")).casefold() in {"ok", "wayback"}
        and bool(str(page.get("text", "") or "").strip())
        for page in record.get("pages", [])
    )


def collect_pages(org, fetch_fn, raw: list[dict], *, deadline: float | None = None) -> list[dict]:
    """Fetch the homepage, follow scored links, then fall back to a snippet."""
    pages: list[dict] = []
    if deadline is not None and time.monotonic() >= deadline:
        return []

    home = _safe_fetch(fetch_fn, org.homepage)
    logger.info("homepage %s -> %s", org.homepage, home.get("status"))

    if home.get("status") in ("ok", "wayback") and home.get("html"):
        pages.extend(_pages_from_fetch(home))
        try:
            links = score_links(
                home["html"], org.homepage, max_links=MAX_PAGES_PER_ORG - 1
            )
        except Exception as exc:
            logger.warning("score_links failed for %s: %s", org.homepage, exc)
            links = []
        links = [(url, score) for url, score in links if url != str(home.get("final_url", ""))]
        links = links[:max(0, MAX_PAGES_PER_ORG - len(pages))]
        if links:
            # Page requests are independent, so fetch a small bounded set in
            # parallel rather than serially multiplying every network timeout.
            from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
            pool = ThreadPoolExecutor(max_workers=min(3, len(links)))
            futures = {
                pool.submit(_safe_fetch, fetch_fn, url): (url, score)
                for url, score in links
                if deadline is None or time.monotonic() < deadline
            }
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                for future in as_completed(futures, timeout=remaining):
                    url, score = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"status": "dead", "reason": str(exc)}
                    logger.info("link %s (score %s) -> %s", url, score, result.get("status"))
                    if result.get("status") in ("ok", "wayback"):
                        pages.extend(_pages_from_fetch(result))
                        pages[:] = pages[:MAX_PAGES_PER_ORG]
            except TimeoutError:
                logger.warning("page-fetch deadline reached for %s", org.org_name)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

    if not pages:
        # Retain the failed official URL/status for review even when the
        # same-domain snippet fallback is also present.
        pages.append({
            "url": str(home.get("final_url") or org.homepage),
            "page_type": "home",
            "status": "dead",
            "http_status": home.get("http_status"),
            "reason": home.get("reason", "page unavailable") or "page unavailable",
            "text": "",
            "html": "",
        })
        snippet = _snippet_page(org, raw, failed_home=home)
        if snippet:
            logger.info("snippet fallback used for %s", org.org_name)
            pages.append(snippet)
        else:
            logger.info("no usable evidence for %s", org.org_name)

    if any(str(page.get("status", "")).casefold() in {"ok", "wayback"} for page in pages):
        return pages[:MAX_PAGES_PER_ORG]
    # Dead-home status and the single discovery snippet are review metadata,
    # not crawled pages, so preserve both even when the crawl page cap is one.
    return pages


def run_discovery(
    plan,
    search_fn=None,
    fetch_fn=None,
    classify_fn=None,
    replan_fn=None,
    enrich: bool = True,
) -> list[dict]:
    """Run bounded discovery and retain candidate records even when pages fail.

    Records with live/archived pages can proceed to people extraction. A
    snippet-only or unfetchable organization is still retained for review, but
    does not count toward the usable-organization target or suppress re-planning.
    """
    search_fn = search_fn or run_query
    fetch_fn = fetch_fn or fetch_page
    classify_fn = classify_fn or classify_results
    replan_fn = replan_fn or propose_queries

    setup_trace()
    category = plan.category
    city = plan.city
    budget_seconds = max(0.1, TIME_BUDGET_MIN * 60.0)
    deadline = time.monotonic() + budget_seconds
    logger.info("discovery start: category=%s city=%s time_budget_seconds=%.0f", category, city, budget_seconds)

    queries: list[str] = list(getattr(plan, "queries", []) or [])
    used: list[str] = []
    raw_all: list[dict] = []
    seen_domains: set[str] = set()
    records: list[dict] = []
    seed_evidence: dict[str, list[dict]] = {}
    rounds = 0

    def usable_count() -> int:
        return sum(1 for record in records if _usable_official_record(record))

    def add_orgs(orgs: list, raw: list[dict], round_number: int) -> None:
        for org in orgs:
            if org.domain in seen_domains:
                continue
            if len(records) >= MAX_CANDIDATES:
                logger.info("MAX_CANDIDATES (%d) reached", MAX_CANDIDATES)
                break
            seen_domains.add(org.domain)
            pages: list[dict] = []
            if usable_count() < MAX_ORGS and time.monotonic() < deadline:
                pages = collect_pages(org, fetch_fn, raw, deadline=deadline)
            elif usable_count() >= MAX_ORGS:
                logger.info("usable organization cap reached; retaining candidate without crawl: %s", org.org_name)
            else:
                logger.info("time budget reached; retaining candidate without crawl: %s", org.org_name)

            if any(str(page.get("status", "")).casefold() in {"ok", "wayback"} for page in pages):
                evidence_status = "official_page_available"
            elif any(str(page.get("status", "")).casefold() == "search_snippet" for page in pages):
                evidence_status = "snippet_only"
            else:
                evidence_status = "no_page_available"

            matched_evidence = []
            for seed_name, sources in seed_evidence.items():
                if result_matches_name(seed_name, {"title": org.org_name, "snippet": ""}, threshold=85):
                    matched_evidence.extend(sources)
            records.append({
                "org": org,
                "pages": pages,
                "verification": summarize_official_evidence(org.domain, raw_all, pages, city),
                "discovery_evidence": matched_evidence,
                "candidate_status": evidence_status,
                "discovered_round": round_number,
            })

    # Resolve brochure/prompt seed organizations directly before broad searches.
    seed_orgs, seed_evidence, seed_queries, seed_rows = _resolve_seed_orgs(plan, search_fn, deadline)
    used.extend(seed_queries)
    raw_all.extend(seed_rows)
    if seed_orgs:
        add_orgs(seed_orgs, raw_all, -1)

    while True:
        if time.monotonic() >= deadline:
            logger.warning("discovery stopped at the configured time budget")
            break
        round_raw: list[dict] = []

        for query in queries:
            if time.monotonic() >= deadline:
                logger.warning("search loop stopped at the configured time budget")
                break
            try:
                results = search_fn(query) or []
            except Exception as exc:
                logger.warning("search failed for %r: %s", query, exc)
                results = []
            used.append(query)
            round_raw.extend(results)
            logger.info("query %r -> %d results", query, len(results))

        if enrich and rounds == 0 and usable_count() < MIN_ORGS and time.monotonic() < deadline:
            enrichment_results, found_seed_evidence, enrichment_queries = _enrich_search_results(
                plan, round_raw, search_fn, deadline=deadline
            )
            used.extend(enrichment_queries)
            seed_evidence.update(found_seed_evidence)
            round_raw = [*enrichment_results, *round_raw]

        round_raw = _dedupe_results(round_raw)
        raw_all.extend(round_raw)
        kept, dropped = filter_results(round_raw)
        drop_counts: dict[str, int] = {}
        for item in dropped:
            reason = str(item.get("drop_reason", "unknown"))
            drop_counts[reason] = drop_counts.get(reason, 0) + 1
        logger.info("filter: kept=%d dropped=%d reasons=%s", len(kept), len(dropped), drop_counts)

        try:
            classified = classify_fn(kept, category, city) or []
        except Exception as exc:
            logger.warning("classification failed: %s", exc)
            classified = []
        logger.info("classified: %d", len(classified))

        try:
            new_orgs = build_orgs(classified, discovered_via=f"round {rounds}")
        except Exception as exc:
            logger.warning("build_orgs failed: %s", exc)
            new_orgs = []
        add_orgs(new_orgs, raw_all, rounds)

        usable = usable_count()
        logger.info("round %d finished: candidates=%d usable_official_page_orgs=%d", rounds, len(records), usable)

        if not should_replan(usable, MIN_ORGS, rounds, MAX_ROUNDS):
            logger.info("no re-plan needed; stopping")
            break
        if time.monotonic() >= deadline:
            logger.info("time budget exhausted before re-planning")
            break

        rounds += 1
        logger.info("low usable-page yield; re-planning (round %d of %d)", rounds, MAX_ROUNDS)
        try:
            queries = replan_fn(category, city, used, usable, MIN_ORGS) or []
        except Exception as exc:
            logger.warning("re-plan failed: %s", exc)
            queries = []
        if not queries:
            logger.info("re-plan returned no validated queries; stopping")
            break
        logger.info("new queries: %s", queries)

    pages_total = sum(len(r["pages"]) for r in records)
    logger.info("discovery done: candidates=%d usable_official_page_orgs=%d pages=%d", len(records), usable_count(), pages_total)
    return records


__all__ = ["run_discovery"]