"""Fetch web pages with fallback ladder.
No LLM is used; dead pages do not stop the pipeline.
"""

import logging
from urllib.parse import urlparse, urlunparse
from typing import Dict, List, Optional

import httpx
import trafilatura
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

_logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; LeadAgent/1.0)"
TIMEOUT = 10.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=8),
    retry=retry_if_exception_type(httpx.TransportError),
)
def _get(url: str) -> Optional[httpx.Response]:
    """GET *url* with retries; return None on failure or HTTP >=400."""
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=TIMEOUT) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                _logger.warning("GET %s returned status %s", url, resp.status_code)
                return None
            return resp
    except httpx.HTTPError as exc:
        _logger.warning("GET %s failed: %s", url, type(exc).__name__)
        return None


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

def fetch_page(url: str) -> Dict[str, Optional[str]]:
    """Fetch *url* with fallbacks; never raises, always returns a result dict."""
    try:
        # Primary attempt
        resp = _get(url)
        if resp and resp.text:
            final_url = str(resp.url)
            html = resp.text
            text = _extract(html, final_url)
            reason = "short text" if len(text) < 300 else ""
            return {"status": "ok", "final_url": final_url, "text": text, "html": html, "reason": reason}
        # Alternate URLs
        for alt in alternate_urls(url):
            resp = _get(alt)
            if resp and resp.text:
                final_url = str(resp.url)
                html = resp.text
                text = _extract(html, final_url)
                reason = "short text" if len(text) < 300 else ""
                return {"status": "ok", "final_url": final_url, "text": text, "html": html, "reason": reason}
        # Wayback fallback
        wb = _wayback(url)
        if wb:
            resp = _get(wb)
            if resp and resp.text:
                final_url = str(resp.url)
                html = resp.text
                text = _extract(html, final_url)
                reason = "short text" if len(text) < 300 else ""
                return {"status": "wayback", "final_url": final_url, "text": text, "html": html, "reason": reason}
        # All attempts failed
        return {"status": "dead", "final_url": None, "text": "", "html": "", "reason": "all fallbacks failed"}
    except Exception as exc:
        _logger.warning("fetch_page exception: %s", type(exc).__name__)
        return {"status": "dead", "final_url": None, "text": "", "html": "", "reason": type(exc).__name__}

__all__ = ["fetch_page"]
