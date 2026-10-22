"""Run a bounded Step 2 sample and save evidence for review.

Preferred low-token mode (one Gemini call maximum):
    python scripts/run_small_sample.py --city Kolkata --industry "renewable energy"

Prompt mode uses one extra Step 1 Gemini call:
    python scripts/run_small_sample.py --prompt "Find renewable energy companies in Kolkata"

The normal search uses at most three broad queries plus up to five exact-name
lookups derived from public LinkedIn/list snippets. At most 30 non-social,
name-grounded results enter one Gemini classification batch; social pages themselves are
never crawled or sent to Gemini. The run considers at most 3 organizations and
fetches at most 2 pages per organization.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
# When launched as ``python scripts/run_small_sample.py``, Python places the
# scripts directory (not the repository root) on sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--prompt", help="Use Step 1 Gemini planning (one extra call)")
    inputs.add_argument("--city", help="City/area for a deterministic, low-token sample")
    parser.add_argument("--industry", help="Industry; required with --city")
    parser.add_argument(
        "--category",
        choices=["Sponsor", "College Delegation"],
        default="Sponsor",
        help="Lead category for deterministic mode (default: Sponsor)",
    )
    parser.add_argument("--max-orgs", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--max-pages", type=int, default=2, choices=range(1, 4))
    parser.add_argument(
        "--no-social-search",
        action="store_true",
        help="Skip the extra public LinkedIn/Instagram search queries",
    )
    parser.add_argument(
        "--output",
        default="outputs/small_sample.json",
        help="Output JSON path (default: outputs/small_sample.json)",
    )
    args = parser.parse_args()
    if args.city and not args.industry and args.category == "Sponsor":
        parser.error("--industry is required for Sponsor mode")
    if args.prompt and args.industry:
        parser.error("--industry is only used with --city")
    return args


def _social_queries(industry: str, city: str) -> list[str]:
    """Build two public search queries; results are evidence only, not crawled."""
    terms = []
    for value in (industry, city):
        words = " ".join(str(value or "").split())
        if words:
            terms.append(words)
    suffix = " ".join(terms)
    if not suffix:
        return []
    return [
        f"site:linkedin.com/company {suffix}",
        f"site:instagram.com {suffix}",
    ]


def _sample_query_set(primary: list[str], category: str, industry: str, city: str) -> list[str]:
    """Add narrow official-site queries to reduce directory/listicle dominance."""
    queries = list(primary)
    if category in ("Sponsor", "Both"):
        candidates = [
            f"{industry} company {city} official",
            f"{city} {industry} CSR",
        ]
        for query in candidates:
            query = " ".join(query.split())
            if len(query.split()) <= 5 and query not in queries:
                queries.append(query)
            if len(queries) >= 3:
                break
    return queries[:3]


def _is_social(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("linkedin.com", "instagram.com"))


def _short_result(item: dict) -> dict:
    return {
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "snippet": (item.get("snippet", "") or "")[:700],
        "is_social_domain": _is_social(item.get("url", "")),
    }


def _verification_summary(org, selected_results: list[dict], social_matches: list[dict], pages: list[dict], city: str) -> dict:
    """Report local and sponsor signals with URLs; never infer either signal."""
    evidence = []
    for row in selected_results:
        host = (urlparse(row.get("url", "")).hostname or "").lower()
        if host == org.domain or host.endswith("." + org.domain):
            evidence.append({"url": row.get("url", ""), "text": f"{row.get('title', '')} {row.get('snippet', '')}"})
    for row in social_matches:
        evidence.append({"url": row.get("url", ""), "text": f"{row.get('title', '')} {row.get('snippet', '')}"})
    for page in pages:
        evidence.append({"url": page.get("url", ""), "text": page.get("text", "") or ""})

    location_sources = []
    sponsor_sources = []
    sponsor_pattern = re.compile(
        r"\b(csr|sponsor(?:ship)?s?|partner(?:ship)?s?|student(?:s)?|college|education|community|grant|donation|funding|scholarship|outreach|youth)\b",
        re.I,
    )
    city_pattern = re.compile(rf"(?<!\w){re.escape(city.strip())}(?!\w)", re.I) if city else None
    for item in evidence:
        text = item["text"]
        if city_pattern and city_pattern.search(text):
            location_sources.append(item["url"])
        if sponsor_pattern.search(text):
            sponsor_sources.append(item["url"])

    location_sources = list(dict.fromkeys(x for x in location_sources if x))
    sponsor_sources = list(dict.fromkeys(x for x in sponsor_sources if x))
    if location_sources and sponsor_sources:
        status = "local_presence_and_sponsor_signal_found"
    elif location_sources:
        status = "local_presence_found_sponsor_signal_not_found"
    else:
        status = "location_unverified"
    return {
        "status": status,
        "target_city": city,
        "location_evidence_urls": location_sources,
        "sponsor_or_community_signal_urls": sponsor_sources,
        "warning": "A keyword signal is not proof of sponsorship willingness; review the cited page.",
    }


def main() -> int:
    args = parse_args()

    # Set hard limits before importing discovery/Gemini modules (they read env
    # variables at import time). A manual plan needs at most one Gemini call;
    # prompt mode allows one planning call plus one classification call.
    os.environ["MAX_CALLS_PER_RUN"] = "2" if args.prompt else "1"
    os.environ["REPLAN_ROUNDS"] = "0"
    os.environ["MAX_ORGS"] = str(args.max_orgs)
    os.environ["MAX_PAGES_PER_ORG"] = str(args.max_pages)

    from agent.schemas import SearchPlan
    from agent.search import run_query
    from agent.step1_input import build_plan, build_queries
    from agent.discovery import run_discovery
    from agent.evidence import (
        canonical_url_key,
        is_social_url,
        instagram_company_name,
        linkedin_company_name,
        listicle_names,
        result_matches_name,
        social_matches_name,
    )
    from agent.filters import filter_results
    from agent.gemini_client import BUDGET

    if args.prompt:
        plan = build_plan(prompt=args.prompt)
        plan = plan.model_copy(update={"queries": list(plan.queries[:1])})
        industry = (plan.industries or ["technology"])[0]
        city = plan.city or "India"
    else:
        industry = args.industry or "colleges"
        city = args.city
        queries = build_queries(args.category, city, [industry], None)
        if not queries:
            print(
                "Could not build a query within the project's 5-word limit. "
                "Try a shorter industry or city.",
                file=sys.stderr,
            )
            return 2
        plan = SearchPlan(
            source="prompt",
            category=args.category,
            city=city,
            industries=[industry],
            queries=queries[:1],
        )

    if not plan.queries:
        print("Step 1 produced no search query; nothing to run.", file=sys.stderr)
        return 2

    plan = plan.model_copy(update={
        "queries": _sample_query_set(plan.queries[:1], plan.category, industry, city)
    })

    print("Small sample limits:")
    print(f"  Broad company queries: {len(plan.queries)} (at most 24 raw results)")
    print("  Public social searches: up to 2; only company-name lookups can enter classification")
    print("  Candidate rows sent to Gemini: at most 30 in one batch")
    print(f"  Maximum Gemini calls this run: {os.environ['MAX_CALLS_PER_RUN']}")
    print(f"  Maximum organizations/pages: {args.max_orgs}/{args.max_pages}")
    print(f"  First query: {plan.queries[0]}")
    if args.prompt:
        print("  Note: prompt mode uses one planning call; candidate classification is capped at one batch.")

    search_log: dict[str, list[dict]] = {}
    for query in plan.queries:
        search_log[query] = run_query(query) or []

    social_log: dict[str, list[dict]] = {}
    if not args.no_social_search and plan.category in ("Sponsor", "Both"):
        for query in _social_queries(industry, city):
            rows = run_query(query) or []
            social_log[query] = [r for r in rows if is_social_url(r.get("url", ""))]

    all_main_results = [row for rows in search_log.values() for row in rows]
    eligible_results, dropped_results = filter_results(all_main_results)
    drop_reasons = dict(Counter(row.get("drop_reason", "unknown") for row in dropped_results))

    # Use list/social snippets to discover company names, then verify each name
    # against non-social search results before it can become a lead.
    seeds: list[tuple[str, str]] = []
    seen_seed_names: set[str] = set()
    for rows in social_log.values():
        for row in rows:
            name = linkedin_company_name(row)
            key = (name or "").casefold()
            if name and key not in seen_seed_names:
                seeds.append((name, "linkedin_company_result"))
                seen_seed_names.add(key)
            if sum(source == "linkedin_company_result" for _, source in seeds) >= 3:
                break
        if sum(source == "linkedin_company_result" for _, source in seeds) >= 3:
            break
    instagram_seed_count = 0
    for rows in social_log.values():
        for row in rows:
            name = instagram_company_name(row)
            key = (name or "").casefold()
            if name and key not in seen_seed_names:
                seeds.append((name, "instagram_profile_result"))
                seen_seed_names.add(key)
                instagram_seed_count += 1
            if instagram_seed_count >= 1:
                break
        if instagram_seed_count >= 1:
            break
    for row in dropped_results:
        if row.get("drop_reason") != "listicle":
            continue
        for name in listicle_names(row.get("snippet", "")):
            key = name.casefold()
            if key not in seen_seed_names:
                seeds.append((name, "listicle_snippet"))
                seen_seed_names.add(key)
            if sum(source == "listicle_snippet" for _, source in seeds) >= 2:
                break
        if sum(source == "listicle_snippet" for _, source in seeds) >= 2:
            break
    seeds = seeds[:5]

    lookup_log: dict[str, list[dict]] = {}
    matched_lookup_results: list[dict] = []
    seed_lookup_summary = []
    for name, source in seeds:
        lookup_query = f'"{name}" {city} official'
        rows = run_query(lookup_query) or []
        lookup_log[lookup_query] = rows
        kept, _ = filter_results(rows)
        matches = [row for row in kept if result_matches_name(name, row)]
        for row in matches[:2]:
            matched_lookup_results.append({
                **row,
                "_seed_name": name,
                "_seed_source": source,
            })
        seed_lookup_summary.append({
            "name": name,
            "source": source,
            "query": lookup_query,
            "name_matched_results": min(len(matches), 2),
        })

    # Prefer exact-name search matches to social/list snippets. Deduplicate URLs
    # and cap the combined set at 30, so classification remains one Gemini call.
    selected_results: list[dict] = []
    seen_urls: set[str] = set()
    for row in [*matched_lookup_results, *eligible_results]:
        key = canonical_url_key(row.get("url", ""))
        if not key or key in seen_urls:
            continue
        selected_results.append(row)
        seen_urls.add(key)
        if len(selected_results) >= 30:
            break

    classification_plan = plan.model_copy(update={"queries": ["bounded_grounded_candidates"]})
    records = run_discovery(
        classification_plan,
        search_fn=lambda _query: selected_results,
    )
    lookup_drop_reasons = Counter()
    for rows in lookup_log.values():
        _, dropped = filter_results(rows)
        lookup_drop_reasons.update(row.get("drop_reason", "unknown") for row in dropped)

    serialized_records = []
    for record in records:
        org = record["org"]
        pages = []
        for page in record.get("pages", []):
            pages.append({
                "url": page.get("url", ""),
                "page_type": page.get("page_type", "other"),
                "status": page.get("status", "unknown"),
                "http_status": page.get("http_status"),
                "reason": page.get("reason", ""),
                "text_chars": len(page.get("text", "") or ""),
                "text_excerpt": (page.get("text", "") or "")[:1500],
            })
        social_matches = [
            _short_result(row)
            for rows in social_log.values()
            for row in rows
            if social_matches_name(org.org_name, row)
        ]
        seed_matches = [
            {"name": name, "source": source}
            for name, source in seeds
            if result_matches_name(name, {"title": org.org_name, "snippet": ""})
        ]
        verification = _verification_summary(
            org, selected_results, social_matches, record.get("pages", []), city
        )
        serialized_records.append({
            "organization": org.model_dump(mode="json"),
            "matched_discovery_seeds": seed_matches,
            "matching_social_evidence": social_matches,
            "verification": verification,
            "pages": pages,
        })

    output = {
        "limits": {
            "max_gemini_calls": int(os.environ["MAX_CALLS_PER_RUN"]),
            "max_primary_search_queries": len(plan.queries),
            "max_results_per_query": 8,
            "max_company_name_lookups": 5,
            "max_name_matched_results_per_lookup": 2,
            "max_candidate_rows_to_classify": 30,
            "max_organizations": args.max_orgs,
            "max_pages_per_organization": args.max_pages,
            "replanning_rounds": 0,
        },
        "search_brief": (ROOT / "data" / "sponsor_search_brief.txt").read_text(encoding="utf-8").format(
            industry=industry,
            city=city,
        ),
        "plan": {
            "category": plan.category,
            "city": plan.city,
            "industries": plan.industries,
            "queries": plan.queries,
        },
        "search_results": {
            query: [_short_result(r) for r in rows]
            for query, rows in search_log.items()
        },
        "social_search_results_evidence_only": {
            query: [_short_result(r) for r in rows]
            for query, rows in social_log.items()
        },
        "company_name_lookups": {
            "seeds": seed_lookup_summary,
            "results": {
                query: [_short_result(r) for r in rows]
                for query, rows in lookup_log.items()
            },
        },
        "diagnostics": {
            "main_search_results": len(all_main_results),
            "eligible_after_filters": len(eligible_results),
            "name_matched_lookup_results": len(matched_lookup_results),
            "selected_rows_for_classification": len(selected_results),
            "filter_drop_reasons": drop_reasons,
            "lookup_filter_drop_reasons": dict(lookup_drop_reasons),
            "offline_mode": os.getenv("OFFLINE", "0") == "1",
            "gemini_key_configured": bool(
                os.getenv("GEMINI_API_KEY")
                and not os.getenv("GEMINI_API_KEY", "").startswith("paste_")
            ),
        },
        "organizations": serialized_records,
        "gemini_usage": {
            "calls": BUDGET.run_calls,
            "cache_hits": BUDGET.cache_hits,
            "tokens_by_tag": BUDGET.tokens,
        },
    }

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWrote {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}")
    print(f"Main results: {len(all_main_results)}; eligible: {len(eligible_results)}")
    print(f"Name-matched lookup results: {len(matched_lookup_results)}")
    print(f"Rows selected for classification: {len(selected_results)} (max 30)")
    print(f"Main filter drops: {drop_reasons}; lookup drops: {dict(lookup_drop_reasons)}")
    print(f"Organizations with usable evidence: {len(serialized_records)}")
    print(f"Gemini calls used: {BUDGET.run_calls}; cached answers: {BUDGET.cache_hits}")
    if not selected_results:
        print("No classification call was needed: no eligible non-social results were found.")
    elif BUDGET.run_calls == 0 and BUDGET.cache_hits == 0:
        print("Eligible results exist, but Gemini returned no cached or successful answer.")
        print("Check OFFLINE=0, that GEMINI_API_KEY is set, and the agent logs for API errors.")
    print("Share this JSON file and the terminal output for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
