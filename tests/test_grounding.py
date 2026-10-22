"""Regression checks for grounded locations and search-ad redirect filtering."""

from unittest.mock import patch

from agent.classify import classify_results
from agent.fetch import _quality_reason
from agent.filters import filter_results
from agent.schemas import ClassifiedBatch, ClassifiedResult


def test_search_ad_redirect_is_not_a_lead_domain():
    result = {
        "title": "Schneider Electric - Energy Technology Company",
        "url": "https://www.bing.com/aclick?u=ad-redirect",
        "snippet": "Energy technology company",
    }
    kept, dropped = filter_results([result])
    assert not kept
    assert dropped[0]["drop_reason"] == "blocked_domain"


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


if __name__ == "__main__":
    test_search_ad_redirect_is_not_a_lead_domain()
    test_city_is_only_kept_when_present_in_result_evidence()
    test_placeholder_copy_is_flagged()
    print("grounding tests passed")
