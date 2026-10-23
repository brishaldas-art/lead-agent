"""Search utilities using DDGS."""

import time
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import List, Dict

from ddgs import DDGS
try:
    from ddgs.exceptions import DDGSException
except Exception:  # fallback if the module is missing
    DDGSException = Exception

_logger = logging.getLogger("agent")

CACHE_DIR = Path("outputs/.search_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# More results per query improves recall across a city/industry search. Keep it
# bounded and configurable because DDG responses and downstream classification
# still consume time and model budget.
MAX_RESULTS = min(max(int(os.getenv("SEARCH_MAX_RESULTS", "10")), 1), 20)
DELAY_SECONDS = 1.5
CACHE_TTL_SECONDS = max(60, int(float(os.getenv("SEARCH_CACHE_TTL_HOURS", "48")) * 3600))


def _cache_path(query: str) -> Path:
    """Return the cache file path for *query*."""
    h = hashlib.sha256(query.encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"


def run_query(query: str) -> List[Dict]:
    """Execute a search, using cache when available."""
    q = query.strip()
    if not q:
        return []
    cache_file = _cache_path(q)
    if cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cached = payload.get("results", [])
                cached_at = float(payload.get("cached_at", cache_file.stat().st_mtime))
                cached_limit = payload.get("max_results")
            else:  # read legacy cache files created before TTL metadata existed
                cached = payload
                cached_at = cache_file.stat().st_mtime
                cached_limit = None
            fresh = (
                time.time() - cached_at <= CACHE_TTL_SECONDS
                and cached_limit == MAX_RESULTS
            )
            if fresh and cached:
                return cached
            # Empty results are not useful cache entries; stale results must be
            # refreshed so an old index response cannot replay indefinitely.
            cache_file.unlink(missing_ok=True)
        except Exception:
            cache_file.unlink(missing_ok=True)
    raw = None
    for attempt in range(2):
        time.sleep(DELAY_SECONDS if attempt == 0 else 0.4)
        try:
            raw = DDGS().text(q, max_results=MAX_RESULTS)
            break
        except DDGSException as exc:
            _logger.warning("DDGS query attempt %d failed for %s: %s", attempt + 1, q, exc)
        except Exception as exc:
            _logger.warning("DDGS query attempt %d errored for %s: %s", attempt + 1, q, exc)
    if raw is None:
        return []
    results: List[Dict] = []
    for r in raw:
        url = r.get("href", "")
        if not url:
            continue
        results.append({
            "title": r.get("title", ""),
            "url": url,
            "snippet": r.get("body", ""),
        })
    if results:
        try:
            cache_file.write_text(
                json.dumps({"cached_at": time.time(), "max_results": MAX_RESULTS, "results": results}),
                encoding="utf-8",
            )
        except Exception:
            pass
    return results


def run_queries(queries: List[str]) -> Dict[str, List[Dict]]:
    """Run multiple queries, skipping duplicates, preserving order."""
    seen = set()
    out: Dict[str, List[Dict]] = {}
    for q in queries:
        if q in seen:
            continue
        seen.add(q)
        out[q] = run_query(q)
    return out
