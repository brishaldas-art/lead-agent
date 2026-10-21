"""Command‑line entry point for Step 1.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.step1_input import build_plan

# Load environment variables once at import time
load_dotenv()


def _print_usage():
    msg = (
        "Usage: main.py [--prompt <text>] [--pdf <path>] [--offline]\n"
        "Provide at least one of --prompt or --pdf."
    )
    print(msg, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--prompt", type=str, help="Free‑form user request")
    parser.add_argument("--pdf", type=str, help="Path to brochure PDF")
    parser.add_argument("--offline", action="store_true", help="Run without Gemini calls")
    args, unknown = parser.parse_known_args()

    if args.offline:
        os.environ["OFFLINE"] = "1"

    if not args.prompt and not args.pdf:
        _print_usage()
        return 1

    if args.pdf and not Path(args.pdf).is_file():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    plan = build_plan(prompt=args.prompt, pdf_path=args.pdf)

    # Output fields
    print(f"Source: {plan.source}")
    print(f"Category: {plan.category}")
    print(f"City: {plan.city if plan.city is not None else 'none'}")
    print(f"Theme: {plan.theme if plan.theme is not None else 'none'}")
    if plan.industries:
        print(f"Industries: {', '.join(plan.industries)}")
    else:
        print("Industries: none")

    seed_cnt = len(plan.seed_orgs)
    print(f"Seed orgs ({seed_cnt}):")
    if seed_cnt:
        for org in plan.seed_orgs:
            print(f"  {org}")
    else:
        print("  none")

    query_cnt = len(plan.queries)
    print(f"Queries ({query_cnt}):")
    if query_cnt:
        for i, q in enumerate(plan.queries, 1):
            print(f"  {i}. {q}")
    else:
        print("  none")

    return 0

if __name__ == "__main__":
    sys.exit(main())
