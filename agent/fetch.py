"""Fetch web pages with fallback ladder.
No LLM is used; dead pages do not stop the pipeline.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from typing import Any, Dict, List, Optional

import httpx
import trafilatura
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

_logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; LeadAgent/1.0)"
TIMEOUT = 4.0
CACHE_DIR = Path(os.getenv("PAGE_CACHE_DIR", "outputs/.page_cache"))
PAGE_CACHE_TTL_SECONDS = max(60, int(float(os.getenv("PAGE_CACHE_TTL_HOURS", "72")) * 3600))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=0.2, max=1.0),
    retry=retry_if_exception_type(httpx.TransportError),
    retry_error_callback=lambda retry_state: None,
)
def _get(url: str) -> Optional[httpx.Response]:
    """GET *url* with bounded retries; preserve HTTP responses including 403."""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=TIMEOUT) as client:
        # Let TransportError reach tenacity; catching HTTPError here would
        # swallow the exceptions the retry policy is meant to handle.
        resp = client.get(url)
        if resp.status_code >= 400:
            _logger.warning("GET %s returned status %s", url, resp.status_code)
        return resp


def alternate_urls(url: str) -> List[str]:
    """Return scheme‑swapped and www‑variant URLs (excluding the original)."""
    parts = urlparse(url)
    alts: List[str] = []
    # Scheme swap
    if parts.scheme in {"http", "https"}:
        swapped = parts._replace(scheme="https" if parts.scheme == "http" else "http")
        alts.append(urlunparse(swapped))
    # www add/remove
    host = parts.netloc
    if host.startswith("www."):
        no_www = parts._replace(netloc=host[4:])
        alts.append(urlunparse(no_www))
    else:
        with_www = parts._replace(netloc="www." + host)
        alts.append(urlunparse(with_www))
    # Remove possible duplicates and the original URL
    unique = []
    for u in alts:
        if u != url and u not in unique:
            unique.append(u)
    return unique


def _extract(html: str, url: str) -> str:
    """Extract main text from *html*; return empty string if extraction fails."""
    try:
        text = trafilatura.extract(html, url=url)
        return text or ""
    except Exception:
        return ""


def _quality_reason(text: str) -> str:
    """Flag very short or obvious template-placeholder text for review."""
    reasons = []
    if len(text) < 300:
        reasons.append("short text")
    lowered = text.lower()
    placeholder_markers = (
        "lorem ipsum",
        "dolore magna aliqua",
        "excepteur sint occaecat",
        "sagittis dia facilisis",
    )
    if any(marker in lowered for marker in placeholder_markers):
        reasons.append("placeholder text detected")
    return "; ".join(reasons)


def _wayback(url: str) -> Optional[str]:
    """Return a Wayback Machine snapshot URL if available, else None."""
    api = f"https://archive.org/wayback/available?url={url}"
    try:
        resp = httpx.get(api, timeout=TIMEOUT)
        data = resp.json()
        closest = data.get("archived_snapshots", {}).get("closest", {})
        if closest.get("available"):
            return closest.get("url")
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Main ladder
# ---------------------------------------------------------------------------

def _fetch_page_uncached(url: str) -> Dict[str, Any]:
    """Fetch *url* with fallbacks; preserve the final HTTP status for review."""
    last_http_status: Optional[int] = None
    try:
        # Direct request, then simple canonical URL alternatives.
        for candidate in [url, *alternate_urls(url)]:
            resp = _get(candidate)
            if resp is None:
                continue
            last_http_status = resp.status_code
            if resp.status_code in (401, 403, 429):
                # Respect explicit denials and rate limits; don't retry other
                # live URL variants of a host that has refused this request.
                break
            if resp.status_code >= 400 or not resp.text:
                continue
            final_url = str(resp.url)
            html = resp.text
            text = _extract(html, final_url)
            reason = _quality_reason(text)
            result = {
                "status": "ok",
                "final_url": final_url,
                "text": text,
                "html": html,
                "reason": reason,
                "http_status": resp.status_code,
            }
            # A thin live page remains primary evidence. If a public archive has
            # a richer copy, attach it separately rather than replacing the live
            # page or treating archival text as current proof.
            if reason:
                archived_url = _wayback(url)
                if archived_url:
                    archived = _get(archived_url)
                    if archived is not None and archived.status_code < 400 and archived.text:
                        archived_html = archived.text
                        archived_text = _extract(archived_html, str(archived.url))
                        if len(archived_text) > len(text) and not _quality_reason(archived_text).endswith("placeholder text detected"):
                            result["archive_fallback"] = {
                                "status": "wayback",
                                "final_url": str(archived.url),
                                "text": archived_text,
                                "html": archived_html,
                                "reason": _quality_reason(archived_text),
                                "http_status": archived.status_code,
                            }
            return result

        # Historical public copy, if one exists. This is not a live-page bypass.
        wb = _wayback(url)
        if wb:
            resp = _get(wb)
            if resp is not None:
                last_http_status = resp.status_code
                if resp.status_code < 400 and resp.text:
                    final_url = str(resp.url)
                    html = resp.text
                    text = _extract(html, final_url)
                    reason = _quality_reason(text)
                    return {
                        "status": "wayback",
                        "final_url": final_url,
                        "text": text,
                        "html": html,
                        "reason": reason,
                        "http_status": resp.status_code,
                    }

        reason = (
            f"HTTP {last_http_status}; direct page unavailable"
            if last_http_status is not None
            else "all fetch attempts failed"
        )
        return {
            "status": "dead",
            "final_url": None,
            "text": "",
            "html": "",
            "reason": reason,
            "http_status": last_http_status,
        }
    except Exception as exc:
        _logger.warning("fetch_page exception: %s", type(exc).__name__)
        return {
            "status": "dead",
            "final_url": None,
            "text": "",
            "html": "",
            "reason": type(exc).__name__,
            "http_status": last_http_status,
        }

def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{key}.json"


def fetch_page(url: str) -> Dict[str, Any]:
    """Fetch a page with a TTL cache; transient failures expire quickly."""
    cache_file = _cache_path(url)
    if cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - float(payload.get("cached_at", cache_file.stat().st_mtime))
            status = str(payload.get("result", {}).get("status", "dead"))
            ttl = PAGE_CACHE_TTL_SECONDS if status in {"ok", "wayback"} else 300
            if age <= ttl:
                return payload["result"]
            cache_file.unlink(missing_ok=True)
        except Exception:
            cache_file.unlink(missing_ok=True)

    result = _fetch_page_uncached(url)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"cached_at": time.time(), "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        _logger.debug("page cache write failed: %s", type(exc).__name__)
    return result


__all__ = ["fetch_page"]
