"""Run a bounded Step 2 sample and save evidence for review.

Preferred low-token mode (one Gemini call maximum):
    python scripts/run_small_sample.py --city Kolkata --industry "renewable energy"

Prompt mode uses one extra Step 1 Gemini call:
    python scripts/run_small_sample.py --prompt "Find renewable energy companies in Kolkata"

The normal search uses at most three broad queries plus up to seven exact-name
lookups derived from public LinkedIn/Instagram/list snippets. At most 30
non-social, name-grounded results enter one Gemini classification batch; social pages themselves are
never crawled or sent to Gemini. The run considers at most 3 organizations and
fetches at most 3 pages per organization by default.

Add --full-pipeline to run Steps 3-5 too. Deterministic city mode skips Step 1,
limits classification to 18 candidates, batches the selected organizations
(up to 5) into one people call and allows at most one fallback call. The
Gemini budget is 3 recorded calls in deterministic city mode.
Google Sheets is explicitly disabled; local exports go to outputs/live_sample.
"""

from __future__ import annotations

import argparse
import json
import os
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
    parser.add_argument("--max-pages", type=int, default=3, choices=range(1, 4))
    parser.add_argument(
        "--no-social-search",
        action="store_true",
        help="Skip the extra public LinkedIn/Instagram search queries",
    )
    parser.add_argument(
        "--full-pipeline",
        action="store_true",
        help="Also run Steps 3-5, with at most 3 Gemini calls for deterministic city mode",
    )
    parser.add_argument(
        "--export-dir",
        default="outputs/live_sample",
        help="Isolated local export directory for --full-pipeline",
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
    """Summarize company-site evidence; retain social matches as discovery-only."""
    from agent.evidence import summarize_official_evidence

    org_domain = org.get("domain", "") if isinstance(org, dict) else getattr(org, "domain", "")
    summary = summarize_official_evidence(
        org_domain,
        selected_results,
        pages,
        city,
    )
    summary["social_discovery_evidence_count"] = len(social_matches)
    return summary


def main() -> int:
    args = parse_args()

    # Full deterministic mode uses one discovery batch, one people batch, and
    # at most one fallback batch. Prompt mode adds exactly one planning call.
    # These caps are set before importing modules that read environment values.
    if args.full_pipeline:
        if not os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY", "").startswith("paste_"):
            print("Full-pipeline live test needs GEMINI_API_KEY in the local .env; no searches or API calls were made.", file=sys.stderr)
            return 2
        if os.getenv("OFFLINE", "0") == "1":
            print("Full-pipeline live test requires OFFLINE=0 in the local .env; no searches or API calls were made.", file=sys.stderr)
            return 2
        max_calls = (4 if args.prompt else 3)
        os.environ["MAX_SNIPPET_FALLBACKS"] = "1"
    else:
        max_calls = 2 if args.prompt else 1
    os.environ["MAX_CALLS_PER_RUN"] = str(max_calls)
    os.environ["REPLAN_ROUNDS"] = "0"
    os.environ["MAX_ORGS"] = str(args.max_orgs)
    os.environ["MAX_PAGES_PER_ORG"] = str(args.max_pages)
    os.environ["BATCH_ORGS"] = str(args.max_orgs)

    from agent.schemas import SearchPlan
    from agent.search import run_query
    from agent.step1_input import build_plan, build_queries
    from agent.discovery import run_discovery
    from agent.evidence import (
        canonical_url_key,
        domain_matches_name,
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
    print("  Public social searches: up to 2; company names are resolved to non-social sites")
    classification_cap = 18 if args.full_pipeline else 30
    print(f"  Candidate rows sent to Gemini: at most {classification_cap} in one batch")
    print(f"  Maximum Gemini calls this run: {os.environ['MAX_CALLS_PER_RUN']}")
    print(f"  Maximum organizations/pages: {args.max_orgs}/{args.max_pages}")
    if args.full_pipeline:
        print("  Full pipeline: one Step 3 fallback at most; Sheets sync disabled; exports isolated")
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
            near_duplicate = any(
                result_matches_name(name, {"title": existing, "snippet": ""}, threshold=85)
                for existing, _source in seeds
            ) if name else False
            if name and key not in seen_seed_names and not near_duplicate:
                seeds.append((name, "instagram_profile_or_post_result"))
                seen_seed_names.add(key)
                instagram_seed_count += 1
            if instagram_seed_count >= 2:
                break
        if instagram_seed_count >= 2:
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
    seeds = seeds[:7]

    lookup_log: dict[str, list[dict]] = {}
    matched_lookup_results: list[dict] = []
    seed_lookup_summary = []
    for name, source in seeds:
        lookup_query = f'"{name}" {city} official'
        rows = run_query(lookup_query) or []
        lookup_log[lookup_query] = rows
        kept, _ = filter_results(rows)
        matches = [
            row for row in kept
            if result_matches_name(name, row) and domain_matches_name(name, row.get("url", ""))
        ]
        for row in matches[:1]:
            matched_lookup_results.append({
                **row,
                "_seed_name": name,
                "_seed_source": source,
            })
        seed_lookup_summary.append({
            "name": name,
            "source": source,
            "query": lookup_query,
            "name_matched_results": min(len(matches), 1),
        })

    # Prefer exact-name search matches to social/list snippets. Deduplicate URLs
    # and cap the combined set to keep classification compact and in one call.
    selected_results: list[dict] = []
    seen_urls: set[str] = set()
    for row in [*matched_lookup_results, *eligible_results]:
        key = canonical_url_key(row.get("url", ""))
        if not key or key in seen_urls:
            continue
        selected_results.append(row)
        seen_urls.add(key)
        if len(selected_results) >= classification_cap:
            break

    classification_plan = plan.model_copy(update={"queries": ["bounded_grounded_candidates"]})
    records = run_discovery(
        classification_plan,
        search_fn=lambda _query: selected_results,
        replan_fn=lambda *_args: [],  # This test runner has a fixed explicit call budget.
        enrich=False,  # The bounded runner already performed and logged its enrichment.
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

    full_pipeline = None
    if args.full_pipeline:
        from agent.step3_people import identify_people
        from agent.step4_contacts import verify_contacts
        from agent.step5_export import export_verified_leads

        people_result = identify_people(
            records,
            batch_orgs=args.max_orgs,
            max_snippet_fallbacks=1,
        )
        contact_result = verify_contacts(people_result, records)
        export_dir = Path(args.export_dir)
        if not export_dir.is_absolute():
            export_dir = ROOT / export_dir
        export_result = export_verified_leads(
            contact_result,
            records,
            output_dir=export_dir,
            sheet_id="",
            credentials_path="",
            run_context={"category": plan.category, "target_location": city},
            people_result=people_result,
        )
        full_pipeline = {
            "decision_makers": [
                {
                    "organization": item.org.model_dump(mode="json"),
                    "person": item.person.model_dump(mode="json"),
                }
                for item in people_result.identified
            ],
            "people_dropped": people_result.dropped,
            "model_batches": people_result.model_batches,
            "snippet_fallbacks_used": people_result.fallback_searches,
            "verified_contacts": [
                {
                    "organization": item.org.model_dump(mode="json"),
                    "person": item.person.model_dump(mode="json"),
                    "contact": item.contact.model_dump(mode="json"),
                }
                for item in contact_result.verified
            ],
            "contacts_dropped": contact_result.dropped,
            "export": {
                "rows_added": export_result.rows_added,
                "total_rows": export_result.total_rows,
                "duplicates_skipped": export_result.duplicates_skipped,
                "dropped": export_result.dropped,
                "csv_path": export_result.csv_path,
                "lead_all_csv_path": export_result.lead_all_csv_path,
                "audit_csv_path": export_result.audit_csv_path,
                "database_path": export_result.database_path,
                "review_candidates": export_result.review_candidates,
                "run_id": export_result.run_id,
                "sheets_status": export_result.sheets_status,
                "sheets_message": export_result.sheets_message,
            },
        }
        print(f"Decision-makers found: {len(people_result.identified)}")
        print(f"Verified contacts: {len(contact_result.verified)}")
        print(f"Step 5 accepted rows: {export_result.rows_added}; dropped: {len(export_result.dropped)}")
        print(f"Local export directory: {export_dir}")

    output = {
        "limits": {
            "max_gemini_calls": int(os.environ["MAX_CALLS_PER_RUN"]),
            "max_primary_search_queries": len(plan.queries),
            "max_results_per_query": 8,
            "max_company_name_lookups": 7,
            "max_name_matched_results_per_lookup": 1,
            "max_candidate_rows_to_classify": classification_cap,
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
        "full_pipeline": full_pipeline,
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
    print(f"Rows selected for classification: {len(selected_results)} (max {classification_cap})")
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
