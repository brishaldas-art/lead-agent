"""Offline coverage for production social/list name resolution."""

from types import SimpleNamespace

from agent.discovery import _enrich_search_results
from agent.filters import filter_results


def test_production_enrichment_resolves_names_without_promoting_social_hosts():
    plan = SimpleNamespace(
        category="Sponsor",
        city="Kolkata",
        industries=["renewable energy"],
    )
    primary = [{
        "title": "Top 10 solar companies in Kolkata",
        "url": "https://industry-blog.example/top-solar",
        "snippet": "Companies including Vega Solar and ONergy Solar.",
    }]
    linkedin = {
        "title": "Jupiter International Limited | LinkedIn",
        "url": "https://www.linkedin.com/company/jupiter-international/",
        "snippet": "Renewable energy company in Kolkata.",
    }
    instagram = {
        "title": "Fender Energy | Solar installation",
        "url": "https://www.instagram.com/reel/123/",
        "snippet": "Fender Energy solar installation in Kolkata.",
    }
    official = {
        "Jupiter International": {
            "title": "Jupiter International Limited - Official",
            "url": "https://jil-jupiter.com/contact",
            "snippet": "Jupiter International corporate office in Kolkata.",
        },
        "Fender Energy": {
            "title": "Fender Energy - Official",
            "url": "https://fenderenergy.in/",
            "snippet": "Fender Energy solar solutions.",
        },
        "Vega Solar": {
            "title": "Vega Solar - Official",
            "url": "https://vegasolar.in/",
            "snippet": "Vega Solar renewable energy solutions.",
        },
        "ONergy Solar": {
            "title": "ONergy Solar - Official",
            "url": "https://onergy.in/",
            "snippet": "ONergy Solar products.",
        },
    }

    def fake_search(query):
        if query.startswith("site:linkedin.com/company"):
            return [linkedin]
        if query.startswith("site:instagram.com"):
            return [instagram]
        for name, result in official.items():
            if name.casefold() in query.casefold():
                return [result]
        return []

    candidates, evidence = _enrich_search_results(plan, primary, fake_search)
    assert {row["url"] for row in candidates} == {row["url"] for row in official.values()}
    assert all("linkedin.com" not in row["url"] and "instagram.com" not in row["url"] for row in candidates)
    assert evidence["Jupiter International Limited"][0]["evidence_type"] == "public_social_discovery_only"
    assert evidence["Vega Solar"][0]["evidence_type"] == "listicle_discovery_only"


def test_top_numbered_pages_are_filtered_as_listicles():
    kept, dropped = filter_results([{
        "title": "Top 10 solar companies in Kolkata",
        "url": "https://industry-blog.example/top-solar",
        "snippet": "Including Vega Solar.",
    }])
    assert not kept
    assert dropped[0]["drop_reason"] == "listicle"


if __name__ == "__main__":
    test_production_enrichment_resolves_names_without_promoting_social_hosts()
    test_top_numbered_pages_are_filtered_as_listicles()
    print("discovery enrichment tests passed")
