"""Search utilities using DDGS."""

import time
import hashlib
import json
import logging
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

MAX_RESULTS = 8
DELAY_SECONDS = 1.5


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
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached:
                return cached
            # Empty search results may have been caused by a transient DDGS
            # failure. Do not let an old miss suppress future live retries.
            cache_file.unlink(missing_ok=True)
        except Exception:
            pass
    time.sleep(DELAY_SECONDS)
    try:
        raw = DDGS().text(q, max_results=MAX_RESULTS)
    except DDGSException as e:
        _logger.warning("DDGS query failed for %s: %s", q, e)
        return []
    except Exception as e:
        _logger.warning("DDGS query error for %s: %s", q, e)
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
            cache_file.write_text(json.dumps(results), encoding="utf-8")
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
