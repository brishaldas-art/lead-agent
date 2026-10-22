"""Classification of search results using Gemini.
Ensures Gemini never supplies URLs; every org name is grounded against the original result.
"""

import logging
import re
from typing import List, Dict

from rapidfuzz import fuzz

from agent.gemini_client import ask
from agent.prompts import CLASSIFY
from agent.schemas import ClassifiedBatch, ClassifiedResult
from agent.filters import filter_results

_logger = logging.getLogger("agent")

BATCH_SIZE = 30


def _truncate(text: str, limit: int = 200) -> str:
    """Trim *text* to *limit* characters, replace newlines with spaces."""
    if not text:
        return ""
    single_line = " ".join(text.splitlines())
    return single_line[:limit]


def _mentions_location(location: str | None, evidence: str) -> bool:
    """Only accept a location when it is literally present in the evidence."""
    if not location:
        return False
    return re.search(rf"(?<!\w){re.escape(location.strip())}(?!\w)", evidence, re.I) is not None


def classify_results(results: List[Dict], category: str, city: str | None) -> List[Dict]:
    """Classify a list of search results and return grounded matches.

    Parameters
    ----------
    results: list of dicts with keys "title", "url", "snippet".
    category: "Sponsor" or other.
    city: optional city string.
    """
    # Step 1: filter
    kept, _ = filter_results(results)
    if not kept:
        return []

    # Map result_id to original result for later lookup
    id_to_item = {idx: item for idx, item in enumerate(kept)}

    outputs: List[Dict] = []
    for start in range(0, len(kept), BATCH_SIZE):
        chunk = kept[start : start + BATCH_SIZE]
        # Build numbered block
        lines = []
        for idx, item in enumerate(chunk, start=start):
            title = item.get("title", "")
            snippet = _truncate(item.get("snippet", ""))
            lines.append(f"{idx} | {title} | {snippet}")
        block = "\n".join(lines)
        prompt = CLASSIFY.format(
            category=category,
            city=city or "unknown",
            numbered_results=block,
        )
        # Call Gemini
        batch: ClassifiedBatch | None = ask(
            ClassifiedBatch,
            prompt,
            tier="fast",
            tag="s2_classify",
            max_out=1500,
        )
        if batch is None:
            _logger.warning("Gemini classification returned no result for a batch.")
            continue
        for res in batch.results:
            # Ensure we have the original item
            original = id_to_item.get(res.result_id)
            if original is None:
                continue
            if not getattr(res, "relevant", False):
                continue
            # Grounding check
            combined = f"{original.get('title', '')} {original.get('snippet', '')}".lower()
            if fuzz.partial_ratio(res.org_name.lower(), combined) < 80:
                continue
            # Do not let the query's target city become an ungrounded claim.
            # Keep the requested city only when the result title/snippet states it.
            verified_city = city if _mentions_location(city, combined) else None
            if city is None and _mentions_location(res.city, combined):
                verified_city = res.city
            output_item = {
                "org_name": res.org_name,
                "category": res.category,
                "city": verified_city,
                "url": original.get("url", ""),
                "title": original.get("title", ""),
                "snippet": original.get("snippet", ""),
            }
            outputs.append(output_item)
    return outputs

__all__ = ["classify_results", "BATCH_SIZE"]
