"""Offline tests for seed extraction from public search-result snippets."""

from agent.evidence import (
    canonical_url_key,
    domain_matches_name,
    instagram_company_name,
    linkedin_company_name,
    listicle_names,
    result_matches_name,
)


def test_linkedin_company_page_title_yields_name():
    result = {
        "title": "Aegis Renewable Tech Solutions Pvt. Ltd. - LinkedIn",
        "url": "https://www.linkedin.com/company/aegis-renewables-pvt-ltd",
    }
    assert linkedin_company_name(result) == "Aegis Renewable Tech Solutions Pvt. Ltd."


def test_instagram_profile_handle_is_seed_but_post_is_not():
    profile = {
        "title": "Leading Solar Company Kolkata (@renewco_solar) - Instagram",
        "url": "https://www.instagram.com/renewco_solar/",
    }
    post = {
        "title": "Debashrita Karuri | Solar Dome Renewable Energy Museum",
        "url": "https://www.instagram.com/reel/abc123/",
        "snippet": "A tour of the renewable energy museum in Kolkata.",
    }
    business_post = {
        "title": "Fender Energy | Strength you can trust",
        "url": "https://www.instagram.com/reel/def456/",
        "snippet": "Fender Energy solar installation in Kolkata.",
    }
    assert instagram_company_name(profile) == "renewco solar"
    assert instagram_company_name(post) is None
    assert instagram_company_name(business_post) == "Fender Energy"


def test_names_are_extracted_only_from_explicit_list_snippet_patterns():
    text = "Discover companies, including ONergy Solar and Nimbus Solar Solutions."
    assert listicle_names(text) == ["ONergy Solar", "Nimbus Solar Solutions"]
    assert listicle_names("The energy market is growing quickly.") == []


def test_lookup_result_must_mention_seed_and_urls_are_deduped():
    assert result_matches_name("Jupiter International Limited", {
        "title": "Jupiter International Ltd - Official website",
        "snippet": "Solar manufacturer based in India",
    })
    assert canonical_url_key("https://www.example.com/About/?utm_source=ddg#team") == "https://example.com/about"
    assert canonical_url_key("http://example.com/about/") == canonical_url_key("https://www.example.com/about")
    assert domain_matches_name("Jupiter International Limited", "https://jil-jupiter.com/contact")
    assert not domain_matches_name("Aegis Renewable Tech Solutions Pvt Ltd", "https://zaubacorp.com/company/aegis")
    assert domain_matches_name("Green Energy Private Limited", "https://greenenergy.in/")


if __name__ == "__main__":
    test_linkedin_company_page_title_yields_name()
    test_instagram_profile_handle_is_seed_but_post_is_not()
    test_names_are_extracted_only_from_explicit_list_snippet_patterns()
    test_lookup_result_must_mention_seed_and_urls_are_deduped()
    print("evidence helper tests passed")
