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

from agent.search import run_query
from agent.filters import filter_results
from agent.classify import classify_results
from agent.orgs import build_orgs
from agent.crawl import score_links
from agent.fetch import fetch_page
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

logger = logging.getLogger("agent.discovery")

MIN_ORGS: int = int(os.getenv("MIN_ORGS", "15"))
MAX_ROUNDS: int = int(os.getenv("REPLAN_ROUNDS", "0"))
MAX_ORGS: int = int(os.getenv("MAX_ORGS", "20"))
MAX_PAGES_PER_ORG: int = int(os.getenv("MAX_PAGES_PER_ORG", "3"))
SEARCH_DELAY_SECONDS: float = 1.5
TRACE_PATH: Path = Path("outputs/agent_trace.log")

_trace_ready: bool = False


def setup_trace() -> None:
    """Attach a file handler for the agent.discovery logger, once per process."""
    global _trace_ready
    if _trace_ready:
        return
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(TRACE_PATH, mode="a", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
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
    return "other"


def _safe_fetch(fetch_fn, url: str) -> dict:
    """Call fetch_fn and never raise. A failure is reported as a dead page."""
    try:
        result = fetch_fn(url)
        return result if isinstance(result, dict) else {"status": "dead"}
    except Exception as exc:
        logger.warning("fetch raised for %s: %s", url, exc)
        return {"status": "dead", "reason": str(exc)}


def _enrich_search_results(plan, primary_results: list[dict], search_fn):
    """Use social/list snippets only to seed bounded company-name searches.

    Social and listicle pages are returned separately as discovery evidence;
    only name-matched, brand-consistent non-social search results are candidates
    for classification and crawling.
    """
    if getattr(plan, "category", "") not in ("Sponsor", "Both"):
        return [], {}

    industries = getattr(plan, "industries", None) or []
    industry = str(industries[0]) if industries else "companies"
    city = str(getattr(plan, "city", None) or "India")
    social_queries = [
        f"site:linkedin.com/company {industry} {city}",
        f"site:instagram.com {industry} {city}",
    ]
    social_rows: list[dict] = []
    for query in social_queries:
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
    for name, source, evidence in seeds[:7]:
        seed_evidence[name] = [{
            "evidence_type": "public_social_discovery_only" if source.startswith(("linkedin", "instagram")) else "listicle_discovery_only",
            "source": evidence.get("url", ""),
            "title": evidence.get("title", ""),
            "snippet": evidence.get("snippet", "") or "",
        }]
        query = f'"{name}" {city} official'
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
        len(social_rows), len(seeds[:7]), len(extra_results),
    )
    return extra_results, seed_evidence


def _page_from_fetch(result: dict) -> dict:
    url = result.get("final_url") or ""
    return {
        "url": url,
        "page_type": _page_type(url),
        "status": result.get("status", "dead"),
        "http_status": result.get("http_status"),
        "reason": result.get("reason", "") or "",
        "text": result.get("text", "") or "",
        "html": result.get("html", "") or "",
    }


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


def collect_pages(org, fetch_fn, raw: list[dict]) -> list[dict]:
    """Fetch the homepage, follow scored links, then fall back to a snippet."""
    pages: list[dict] = []

    home = _safe_fetch(fetch_fn, org.homepage)
    logger.info("homepage %s -> %s", org.homepage, home.get("status"))

    if home.get("status") in ("ok", "wayback") and home.get("html"):
        pages.append(_page_from_fetch(home))
        try:
            links = score_links(
                home["html"], org.homepage, max_links=MAX_PAGES_PER_ORG - 1
            )
        except Exception as exc:
            logger.warning("score_links failed for %s: %s", org.homepage, exc)
            links = []
        for url, score in links:
            if len(pages) >= MAX_PAGES_PER_ORG:
                break
            result = _safe_fetch(fetch_fn, url)
            logger.info("link %s (score %s) -> %s", url, score, result.get("status"))
            if result.get("status") in ("ok", "wayback"):
                pages.append(_page_from_fetch(result))

    if not pages:
        snippet = _snippet_page(org, raw, failed_home=home)
        if snippet:
            logger.info("snippet fallback used for %s", org.org_name)
            pages.append(snippet)
        else:
            logger.info("no evidence for %s", org.org_name)

    return pages[:MAX_PAGES_PER_ORG]


def run_discovery(
    plan,
    search_fn=None,
    fetch_fn=None,
    classify_fn=None,
    replan_fn=None,
    enrich: bool = True,
) -> list[dict]:
    """Run the discovery loop for a SearchPlan.

    Returns a list of {"org": OrgCandidate, "pages": [page dicts]}, one per org
    that has at least one usable page.
    """
    search_fn = search_fn or run_query
    fetch_fn = fetch_fn or fetch_page
    classify_fn = classify_fn or classify_results
    replan_fn = replan_fn or propose_queries

    setup_trace()
    category = plan.category
    city = plan.city
    logger.info("discovery start: category=%s city=%s", category, city)

    queries: list[str] = list(getattr(plan, "queries", []) or [])
    used: list[str] = []
    raw_all: list[dict] = []
    seen_domains: set[str] = set()
    records: list[dict] = []
    seed_evidence: dict[str, list[dict]] = {}
    rounds = 0

    while True:
        round_raw: list[dict] = []

        for i, query in enumerate(queries):
            if i:
                time.sleep(SEARCH_DELAY_SECONDS)
            try:
                results = search_fn(query) or []
            except Exception as exc:
                logger.warning("search failed for %r: %s", query, exc)
                results = []
            used.append(query)
            round_raw.extend(results)
            logger.info("query %r -> %d results", query, len(results))

        enrichment_results: list[dict] = []
        if enrich and rounds == 0:
            enrichment_results, seed_evidence = _enrich_search_results(
                plan, round_raw, search_fn
            )
            # Exact-name/domain-checked results take priority, but social and
            # listicle snippets never enter the classifier or page crawler.
            round_raw = [*enrichment_results, *round_raw]

        raw_all.extend(round_raw)

        kept, dropped = filter_results(round_raw)
        if enrichment_results:
            unique_kept = []
            seen_result_urls = set()
            for row in kept:
                key = canonical_url_key(row.get("url", ""))
                if not key or key in seen_result_urls:
                    continue
                seen_result_urls.add(key)
                unique_kept.append(row)
                if len(unique_kept) >= 30:
                    break
            kept = unique_kept
        logger.info("filter: kept=%d dropped=%d", len(kept), len(dropped))

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

        for org in new_orgs:
            if org.domain in seen_domains:
                continue
            if len(records) >= MAX_ORGS:
                logger.info("MAX_ORGS (%d) reached", MAX_ORGS)
                break
            seen_domains.add(org.domain)
            pages = collect_pages(org, fetch_fn, raw_all)
            if pages:
                matched_evidence = []
                for seed_name, sources in seed_evidence.items():
                    if result_matches_name(
                        seed_name,
                        {"title": org.org_name, "snippet": ""},
                        threshold=85,
                    ):
                        matched_evidence.extend(sources)
                records.append({
                    "org": org,
                    "pages": pages,
                    "verification": summarize_official_evidence(
                        org.domain,
                        raw_all,
                        pages,
                        city,
                    ),
                    "discovery_evidence": matched_evidence,
                })
            else:
                logger.info("dropped %s: no usable pages", org.org_name)

        usable = len(records)
        logger.info("round %d finished: usable orgs=%d", rounds, usable)

        if not should_replan(usable, MIN_ORGS, rounds, MAX_ROUNDS):
            logger.info("no re-plan needed; stopping")
            break

        rounds += 1
        logger.info("low yield, re-planning (round %d of %d)", rounds, MAX_ROUNDS)
        try:
            queries = replan_fn(category, city, used, usable, MIN_ORGS) or []
        except Exception as exc:
            logger.warning("re-plan failed: %s", exc)
            queries = []
        if not queries:
            logger.info("re-plan returned no queries; stopping")
            break
        logger.info("new queries: %s", queries)

    pages_total = sum(len(r["pages"]) for r in records)
    logger.info("discovery done: orgs=%d pages=%d", len(records), pages_total)
    return records


__all__ = ["run_discovery"]