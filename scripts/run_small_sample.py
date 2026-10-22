"""Run a bounded Step 2 sample and save evidence for review.

Preferred low-token mode (one Gemini call maximum):
    python scripts/run_small_sample.py --city Kolkata --industry "renewable energy"

Prompt mode uses one extra Step 1 Gemini call:
    python scripts/run_small_sample.py --prompt "Find renewable energy companies in Kolkata"

The normal search uses at most one query (8 DDGS results), classifies only those
results in one batch, considers at most 3 organizations, and fetches at most 2
pages per organization. Optional LinkedIn/Instagram searches do not go to Gemini.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
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

    print("Small sample limits:")
    print(f"  Search queries sent to classification: {len(plan.queries)}")
    print("  Search results per query: at most 8")
    print(f"  Maximum Gemini calls this run: {os.environ['MAX_CALLS_PER_RUN']}")
    print(f"  Maximum organizations/pages: {args.max_orgs}/{args.max_pages}")
    print(f"  Query: {plan.queries[0]}")
    if args.prompt:
        print("  Note: prompt mode uses one call for planning; classification is capped at one batch.")

    search_log: dict[str, list[dict]] = {}

    def logged_search(query: str) -> list[dict]:
        rows = run_query(query) or []
        search_log[query] = rows
        return rows

    records = run_discovery(plan, search_fn=logged_search)

    social_log: dict[str, list[dict]] = {}
    if not args.no_social_search and plan.category in ("Sponsor", "Both"):
        for query in _social_queries(industry, city):
            rows = run_query(query) or []
            # Only retain results actually hosted by LinkedIn or Instagram.
            social_log[query] = [r for r in rows if _is_social(r.get("url", ""))]

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
        serialized_records.append({
            "organization": org.model_dump(mode="json"),
            "pages": pages,
        })

    output = {
        "limits": {
            "max_gemini_calls": int(os.environ["MAX_CALLS_PER_RUN"]),
            "max_queries_classified": len(plan.queries),
            "max_results_per_query": 8,
            "max_organizations": args.max_orgs,
            "max_pages_per_organization": args.max_pages,
            "replanning_rounds": 0,
        },
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
    print(f"Organizations with usable evidence: {len(serialized_records)}")
    print(f"Gemini calls used: {BUDGET.run_calls}; cached answers: {BUDGET.cache_hits}")
    print("Share this JSON file and the terminal output for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
