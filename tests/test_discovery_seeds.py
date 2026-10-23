"""Brochure seeds resolve first, without a Gemini classification call."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.discovery import run_discovery


def test_seed_orgs_are_resolved_before_broad_queries():
    calls = []
    plan = SimpleNamespace(
        category="Both",
        city="Kolkata",
        queries=[],
        seed_orgs=["Acme Systems"],
    )

    def search(query):
        calls.append(query)
        return [{
            "title": "Acme Systems Official Website",
            "url": "https://acmesystems.in/",
            "snippet": "Acme Systems software company in Kolkata",
        }]

    def fetch(url):
        return {
            "status": "ok",
            "final_url": url,
            "text": "Acme Systems software company, Kolkata office.",
            "html": "<html><body>Acme Systems</body></html>",
            "reason": "",
            "http_status": 200,
        }

    with patch("agent.discovery.MIN_ORGS", 1):
        records = run_discovery(
            plan,
            search_fn=search,
            fetch_fn=fetch,
            classify_fn=lambda *_args: [],
            replan_fn=lambda *_args: [],
            enrich=False,
        )

    assert calls == ['"Acme Systems" Kolkata official']
    assert len(records) == 1
    assert records[0]["org"].org_name == "Acme Systems"
    assert records[0]["org"].discovered_via == "brochure_seed"
    assert records[0]["candidate_status"] == "official_page_available"
    assert records[0]["discovery_evidence"][0]["evidence_type"] == "brochure_seed_domain_resolution"


if __name__ == "__main__":
    test_seed_orgs_are_resolved_before_broad_queries()
    print("Brochure seed discovery tests passed; no live calls were made.")
