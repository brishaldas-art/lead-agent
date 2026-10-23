"""Offline checks that transient empty searches are not cached permanently."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.search as search


class FailingDDGS:
    def text(self, query, max_results):
        raise RuntimeError("temporary no-results response")


class SuccessfulDDGS:
    def text(self, query, max_results):
        return [{
            "href": "https://example.org/",
            "title": "Example Technology",
            "body": "Official technology company",
        }]


def test_transient_empty_search_is_retried_on_next_run():
    query = "technology companies Kolkata"
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(search, "CACHE_DIR", Path(temp_dir)), patch.object(search, "DELAY_SECONDS", 0):
            with patch.object(search, "DDGS", return_value=FailingDDGS()):
                assert search.run_query(query) == []
            cache_path = search._cache_path(query)
            assert not cache_path.exists()

            with patch.object(search, "DDGS", return_value=SuccessfulDDGS()):
                rows = search.run_query(query)
            assert len(rows) == 1
            assert rows[0]["url"] == "https://example.org/"
            assert cache_path.exists()


def test_previously_cached_empty_search_is_not_a_permanent_miss():
    query = "software companies Kolkata"
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch.object(search, "CACHE_DIR", Path(temp_dir)), patch.object(search, "DELAY_SECONDS", 0):
            cache_path = search._cache_path(query)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("[]", encoding="utf-8")
            with patch.object(search, "DDGS", return_value=SuccessfulDDGS()):
                rows = search.run_query(query)
            assert len(rows) == 1
            assert rows[0]["title"] == "Example Technology"


if __name__ == "__main__":
    test_transient_empty_search_is_retried_on_next_run()
    test_previously_cached_empty_search_is_not_a_permanent_miss()
    print("Search cache tests passed; no live search or network calls were made.")
