"""Offline tests for fetch retries and the page cache."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import agent.fetch as fetch


def test_transport_error_retries_and_recovers():
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ConnectTimeout("temporary timeout", request=httpx.Request("GET", url))
            return SimpleNamespace(status_code=200, text="ok", url=url)

    with patch("agent.fetch.httpx.Client", FakeClient):
        response = fetch._get("https://example.org/")
    assert response.status_code == 200
    assert calls["count"] == 2


def test_page_cache_reuses_successful_fetch():
    result = {
        "status": "ok",
        "final_url": "https://example.org/",
        "text": "Example page text",
        "html": "<title>Example</title>",
        "reason": "",
        "http_status": 200,
    }
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(fetch, "CACHE_DIR", Path(directory)):
            with patch.object(fetch, "_fetch_page_uncached", return_value=result) as request:
                assert fetch.fetch_page("https://example.org/") == result
                assert fetch.fetch_page("https://example.org/") == result
                assert request.call_count == 1


def test_expired_page_cache_is_refetched():
    result = {"status": "ok", "final_url": "https://example.org/", "text": "new", "html": "", "reason": "", "http_status": 200}
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(fetch, "CACHE_DIR", Path(directory)), patch.object(fetch, "PAGE_CACHE_TTL_SECONDS", 60):
            cache = fetch._cache_path("https://example.org/")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text('{"cached_at": 1, "result": {"status": "ok", "text": "stale"}}', encoding="utf-8")
            with patch.object(fetch, "_fetch_page_uncached", return_value=result) as request:
                assert fetch.fetch_page("https://example.org/") == result
                assert request.call_count == 1


if __name__ == "__main__":
    test_transport_error_retries_and_recovers()
    test_page_cache_reuses_successful_fetch()
    test_expired_page_cache_is_refetched()
    print("Fetch tests passed; no live network calls were made.")
