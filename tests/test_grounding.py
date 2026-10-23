"""Regression checks for grounded locations and search-ad redirect filtering."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.classify import classify_results
from agent.fetch import _quality_reason, fetch_page
from agent.filters import filter_results
from agent.orgs import build_orgs
from agent.schemas import ClassifiedBatch, ClassifiedResult
from scripts.run_small_sample import _verification_summary


def test_search_ad_redirect_is_not_a_lead_domain():
    result = {
        "title": "Schneider Electric - Energy Technology Company",
        "url": "https://www.bing.com/aclick?u=ad-redirect",
        "snippet": "Energy technology company",
    }
    kept, dropped = filter_results([result])
    assert not kept
    assert dropped[0]["drop_reason"] == "blocked_domain"


def test_csr_publisher_is_filtered_as_a_discovery_source():
    result = {
        "title": "Top CSR projects in Kolkata",
        "url": "https://thecsrjournal.in/top-csr-kolkata",
        "snippet": "News and analysis of CSR programs.",
    }
    kept, dropped = filter_results([result])
    assert not kept
    assert dropped[0]["drop_reason"] == "blocked_domain"


def test_classifier_sees_source_host_and_sponsor_exclusion_rules():
    result = {
        "title": "Welcome to WBREDA",
        "url": "https://wbreda.org/",
        "snippet": "State nodal agency for renewable energy programs.",
    }
    batch = ClassifiedBatch(results=[ClassifiedResult(
        result_id=0,
        relevant=False,
        org_name="WBREDA",
        category="Sponsor",
        city=None,
    )])
    with patch("agent.classify.ask", return_value=batch) as ask:
        assert classify_results([result], "Sponsor", "Kolkata") == []
    prompt = ask.call_args.args[1]
    assert "source: wbreda.org" in prompt
    assert "government departments/agencies" in prompt
    assert "news publishers" in prompt


def test_city_is_only_kept_when_present_in_result_evidence():
    batch = ClassifiedBatch(results=[ClassifiedResult(
        result_id=0,
        relevant=True,
        org_name="Acme Solar",
        category="Sponsor",
        city="Kolkata",
    )])
    result = {
        "title": "Acme Solar - Official site",
        "url": "https://acmesolar.example/",
        "snippet": "Solar energy services across India.",
    }
    with patch("agent.classify.ask", return_value=batch):
        unverified = classify_results([result], "Sponsor", "Kolkata")
    assert len(unverified) == 1
    assert unverified[0]["city"] is None

    result["snippet"] = "Solar energy services in Kolkata."
    with patch("agent.classify.ask", return_value=batch):
        verified = classify_results([result], "Sponsor", "Kolkata")
    assert verified[0]["city"] == "Kolkata"


def test_placeholder_copy_is_flagged():
    text = "Solar solutions. " * 30 + "Dolore magna aliqua"
    assert "placeholder text detected" in _quality_reason(text)


def test_directory_host_is_not_used_as_company_homepage():
    candidates = [{
        "org_name": "Aegis Renewable Tech Solutions Private Limited",
        "category": "Sponsor",
        "city": "Kolkata",
        "url": "https://www.zaubacorp.com/company/AEGIS-RENEWABLE-TECH-SOLUTIONS",
    }]
    assert build_orgs(candidates) == []
    candidates[0]["url"] = "https://aegisrenewables.com/"
    assert len(build_orgs(candidates)) == 1


def test_403_stops_live_url_variant_retries():
    blocked = SimpleNamespace(status_code=403, text="forbidden", url="https://example.com/")
    with patch("agent.fetch._get", return_value=blocked) as get, patch("agent.fetch._wayback", return_value=None):
        result = fetch_page("https://example.com/")
    assert result["status"] == "dead"
    assert result["http_status"] == 403
    assert get.call_count == 1


def test_csr_keyword_does_not_count_as_direct_sponsorship():
    org = {"domain": "jil-jupiter.com"}
    sources = [{
        "url": "https://jil-jupiter.com/esg",
        "title": "ESG and CSR",
        "snippet": "Kolkata office; community education and CSR initiatives.",
    }]
    summary = _verification_summary(org, sources, [], [], "Kolkata")
    assert summary["status"] == "local_presence_and_related_csr_signal_only"
    assert summary["direct_sponsorship_evidence_urls"] == []
    assert summary["related_csr_community_evidence_urls"] == [sources[0]["url"]]

    social_only = [{
        "url": "https://www.linkedin.com/company/jupiter-international/",
        "title": "Jupiter International | LinkedIn",
        "snippet": "Kolkata company sponsoring local college fests.",
    }]
    summary = _verification_summary(org, [], social_only, [], "Kolkata")
    assert summary["status"] == "location_unverified"
    assert summary["direct_sponsorship_evidence_urls"] == []
    assert summary["social_discovery_evidence_count"] == 1

    direct_source = {
        "url": "https://jil-jupiter.com/community",
        "title": "Community partnerships",
        "snippet": "Kolkata office; Jupiter sponsors local college fests.",
    }
    summary = _verification_summary(org, [direct_source], [], [], "Kolkata")
    assert summary["status"] == "local_presence_and_direct_sponsorship_evidence"
    assert summary["direct_sponsorship_evidence_urls"] == [direct_source["url"]]


if __name__ == "__main__":
    test_search_ad_redirect_is_not_a_lead_domain()
    test_csr_publisher_is_filtered_as_a_discovery_source()
    test_classifier_sees_source_host_and_sponsor_exclusion_rules()
    test_city_is_only_kept_when_present_in_result_evidence()
    test_placeholder_copy_is_flagged()
    print("grounding tests passed")
