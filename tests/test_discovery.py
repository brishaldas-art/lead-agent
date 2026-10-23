import os
from types import SimpleNamespace

os.environ["OFFLINE"] = "1"

from agent.discovery import _dedupe_results, run_discovery

def test_duplicate_company_domains_are_removed_before_classification():
    rows = _dedupe_results([
        {"title": "Acme Systems", "url": "http://www.acmesystems.in/", "snippet": "first"},
        {"title": "Acme Systems Team", "url": "https://acmesystems.in/team", "snippet": "second"},
    ])
    assert len(rows) == 1


plan = SimpleNamespace(
    category="Sponsor",
    city="Bengaluru",
    queries=["tech companies sponsor college fest bengaluru"],
    seed_orgs=[],
)

def fake_search(query):
    return [
        {"title": "Acme Systems - Official Site", "url": "https://www.acmesystems.in/", "snippet": "Acme Systems sponsors college fests in Bengaluru"},
        {"title": "Top 10 sponsors 2026", "url": "https://blog.com/top", "snippet": "list of companies"},
    ]

def fake_fetch(url):
    # Homepage is dead, so the snippet fallback must be used.
    return {"status": "dead", "final_url": url, "text": "", "html": "", "reason": "fake"}

def fake_classify(results, category, city):
    return [
        {"org_name": "Acme Systems", "category": category, "city": city,
         "url": r["url"], "title": r["title"], "snippet": r["snippet"]}
        for r in results
    ]

out = run_discovery(
    plan,
    search_fn=fake_search,
    fetch_fn=fake_fetch,
    classify_fn=fake_classify,
    replan_fn=lambda *args: [],
    enrich=False,
)
for item in out:
    print(item["org"].org_name, item["org"].domain, [(p["status"], p["url"]) for p in item["pages"]])
assert len(out) == 1
assert [page["status"] for page in out[0]["pages"]] == ["dead", "search_snippet"]
assert out[0]["candidate_status"] == "snippet_only"
assert out[0]["verification"]["status"] == "location_unverified"
assert out[0]["verification"]["direct_sponsorship_evidence_urls"] == []
assert out[0]["discovery_evidence"] == []
test_duplicate_company_domains_are_removed_before_classification()
print("discovery tests passed")