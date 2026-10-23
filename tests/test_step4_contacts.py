"""Offline Step 4 tests using fake MX and profile-fetch functions only."""

from agent.schemas import OrgCandidate
from agent.step3_people import OrgWithPerson, Person, Step3Outcome
from agent.step4_contacts import (
    decode_cfemail,
    email_matches_person,
    extract_page_emails,
    is_generic_inbox,
    verify_contacts,
)


def make_org(domain="acmesolar.in"):
    return OrgCandidate(
        org_name="Acme Solar",
        category="Sponsor",
        city="Kolkata",
        domain=domain,
        homepage=f"https://{domain}/",
        discovered_via="offline-test",
    )


def make_person(name="Asha Banerjee"):
    return Person(
        full_name=name,
        role="Head of CSR",
        source_url="https://acmesolar.in/team",
        role_score=550,
    )


def make_pipeline(org, person, pages):
    return (
        Step3Outcome(identified=[OrgWithPerson(org=org, person=person, all_candidates=[person])]),
        [{"org": org, "pages": pages}],
    )


def cfemail_hex(address, key=0x23):
    data = bytes([key]) + bytes(ord(char) ^ key for char in address)
    return data.hex()


def test_email_extractors_cover_mailto_obfuscation_and_cloudflare():
    encoded = cfemail_hex("asha.banerjee@acmesolar.in")
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Email: help [at] acmesolar [dot] in; Personal: asha(at)acmesolar(dot)in",
        "html": (
            '<a href="mailto:asha.banerjee@acmesolar.in">Email</a>'
            f'<span class="__cf_email__" data-cfemail="{encoded}">[email protected]</span>'
        ),
    }
    emails = {item.address for item in extract_page_emails(page)}
    assert emails == {
        "help@acmesolar.in",
        "asha@acmesolar.in",
        "asha.banerjee@acmesolar.in",
    }
    assert decode_cfemail(encoded) == "asha.banerjee@acmesolar.in"
    assert decode_cfemail("not-hex") is None


def test_exact_person_email_requires_official_domain_name_match_and_mx():
    org = make_org()
    person = make_person()
    page = {
        "url": "https://www.acmesolar.in/contact",
        "status": "ok",
        "page_type": "contact",
        "text": "Contact Asha Banerjee, Head of CSR.",
        "html": '<a href="mailto:asha.banerjee@acmesolar.in">Asha Banerjee</a>',
    }
    step3, records = make_pipeline(org, person, [page])
    mx_domains = []
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda domain: mx_domains.append(domain) or True,
        profile_fetch_fn=lambda _url: None,
    )
    assert len(result.verified) == 1
    contact = result.verified[0].contact
    assert contact.channel == "asha.banerjee@acmesolar.in"
    assert contact.channel_type == "email"
    assert contact.status == "Verified"
    assert str(contact.evidence_url) == page["url"]
    assert [str(url) for url in contact.supporting_urls] == [page["url"]]
    assert "mx_ok" in contact.checks_passed
    assert mx_domains == ["acmesolar.in"]
    assert result.dropped == []


def test_generic_or_foreign_email_is_rejected_before_profile_fallback():
    org = make_org()
    person = make_person()
    page_url = "https://acmesolar.in/contact"
    profile_url = "https://www.linkedin.com/in/asha-banerjee/"
    page = {
        "url": page_url,
        "status": "ok",
        "page_type": "contact",
        "text": "Reach our sales desk or find Asha Banerjee on LinkedIn.",
        "html": (
            '<a href="mailto:sales@acmesolar.in">Sales</a>'
            '<a href="mailto:asha.banerjee@gmail.com">Asha</a>'
            f'<a href="{profile_url}">Professional profile</a>'
        ),
    }
    step3, records = make_pipeline(org, person, [page])
    mx_calls = []

    def fetch_profile(url):
        assert url == profile_url
        return {"status_code": 200, "html": "<title>Asha Banerjee | CSR Lead</title><p>Acme Solar team</p>"}

    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda domain: mx_calls.append(domain) or True,
        profile_fetch_fn=fetch_profile,
    )
    assert len(result.verified) == 1
    contact = result.verified[0].contact
    assert contact.channel == profile_url
    assert contact.channel_type == "profile_link"
    assert contact.status == "Verified"
    assert str(contact.evidence_url) == page_url
    assert [str(url) for url in contact.supporting_urls] == [page_url, profile_url]
    assert "http_200" in contact.checks_passed
    assert mx_calls == []
    assert is_generic_inbox("sales@acmesolar.in")
    assert is_generic_inbox("no.reply@acmesolar.in")


def test_company_level_contact_fallback_when_no_person_specific_channel_passes():
    org = make_org()
    person = make_person()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Asha Banerjee is on our team. Contact partnerships@acmesolar.in.",
        "html": '<a href="mailto:partnerships@acmesolar.in">Partnerships</a>',
    }
    step3, records = make_pipeline(org, person, [page])
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda domain: domain == "acmesolar.in",
        profile_fetch_fn=lambda _url: None,
    )
    assert len(result.verified) == 1
    assert result.verified[0].person.full_name == "General Contact"
    assert result.verified[0].person.role == "Company Contact"
    assert result.verified[0].contact.channel == "partnerships@acmesolar.in"


def test_email_without_mx_is_not_marked_verified():
    org = make_org()
    person = make_person()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Asha Banerjee asha.banerjee@acmesolar.in",
        "html": '<a href="mailto:asha.banerjee@acmesolar.in">Asha</a>',
    }
    step3, records = make_pipeline(org, person, [page])
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda _domain: False,
        profile_fetch_fn=lambda _url: None,
    )
    assert result.verified == []
    assert "no verified MX" in result.dropped[0]["reason"]


def test_profile_fallback_rejects_non_200_responses():
    org = make_org()
    person = make_person()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Asha Banerjee profile",
        "html": '<a href="https://www.linkedin.com/in/asha-banerjee/">Profile</a>',
    }
    step3, records = make_pipeline(org, person, [page])
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda _domain: True,
        profile_fetch_fn=lambda _url: {"status_code": 403, "html": "Asha Banerjee"},
    )
    assert result.verified == []
    assert "no verifiable" in result.dropped[0]["reason"]

    unrelated = verify_contacts(
        step3,
        records,
        mx_lookup=lambda _domain: True,
        profile_fetch_fn=lambda _url: {
            "status_code": 200,
            "html": "<title>LinkedIn Login</title><p>Sign in to continue</p>",
        },
    )
    assert unrelated.verified == []


def test_pattern_derived_email_requires_two_same_domain_examples_and_mx():
    org = make_org()
    person = make_person()
    page_url = "https://acmesolar.in/leadership"
    page = {
        "url": page_url,
        "status": "ok",
        "page_type": "team",
        "text": "Team email addresses: maya.sen@acmesolar.in and raj.kumar@acmesolar.in.",
        "html": "<p>maya.sen@acmesolar.in<br>raj.kumar@acmesolar.in</p>",
    }
    single_example_page = {
        **page,
        "text": "Team email address: maya.sen@acmesolar.in.",
        "html": "<p>maya.sen@acmesolar.in</p>",
    }
    single_step3, single_records = make_pipeline(org, person, [single_example_page])
    single_result = verify_contacts(
        single_step3,
        single_records,
        mx_lookup=lambda _domain: True,
        profile_fetch_fn=lambda _url: None,
    )
    assert single_result.verified == []

    step3, records = make_pipeline(org, person, [page])
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda domain: domain == "acmesolar.in",
        profile_fetch_fn=lambda _url: None,
    )
    assert len(result.verified) == 1
    contact = result.verified[0].contact
    assert contact.channel == "asha.banerjee@acmesolar.in"
    assert contact.status == "Pattern-Derived"
    assert [str(url) for url in contact.supporting_urls] == [page_url]
    assert "pattern_observed_on_same_domain" in contact.checks_passed
    assert "pattern_examples:2" in contact.checks_passed


def test_wayback_only_emails_and_unmatched_contacts_are_not_verified():
    org = make_org()
    person = make_person()
    page = {
        "url": "https://web.archive.org/web/20260101/https://acmesolar.in/team",
        "status": "wayback",
        "page_type": "team",
        "text": "Asha Banerjee asha.banerjee@acmesolar.in",
        "html": '<a href="mailto:asha.banerjee@acmesolar.in">Email</a>',
    }
    step3, records = make_pipeline(org, person, [page])
    result = verify_contacts(
        step3,
        records,
        mx_lookup=lambda _domain: True,
        profile_fetch_fn=lambda _url: None,
    )
    assert result.verified == []
    assert len(result.dropped) == 1
    assert "no live official-domain pages" in result.dropped[0]["reason"]


def test_public_generic_contact_qualifies_as_company_level_without_a_person():
    org = make_org()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Contact us at info@acmesolar.in",
        "html": '<a href="mailto:info@acmesolar.in">Contact</a>',
    }
    result = verify_contacts(
        Step3Outcome(),
        [{"org": org, "pages": [page]}],
        mx_lookup=lambda domain: domain == "acmesolar.in",
        profile_fetch_fn=lambda _url: None,
    )
    assert len(result.verified) == 1
    lead = result.verified[0]
    assert lead.person.full_name == "General Contact"
    assert lead.person.role == "Company Contact"
    assert lead.contact.channel == "info@acmesolar.in"
    assert lead.contact.status == "Verified"
    assert str(lead.contact.evidence_url) == page["url"]
    assert {
        "published_on_live_official_page",
        "company_level_shared_inbox",
        "same_org_domain",
        "syntax_valid",
        "mx_ok",
    }.issubset(lead.contact.checks_passed)
    assert result.dropped == []
    assert result.observed_contacts[0]["channel"] == "info@acmesolar.in"
    assert "generic inbox" in result.observed_contacts[0]["verification_status"]
    assert result.observed_contacts[0]["evidence_urls"] == [page["url"]]


def test_company_level_contact_fallback_requires_live_same_domain_and_mx():
    org = make_org()
    official_page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Contact our partnerships team at partnerships@acmesolar.in",
        "html": '<a href="mailto:partnerships@acmesolar.in">Partnerships</a>',
    }
    for page, mx_lookup in [
        ({**official_page, "status": "wayback"}, lambda _domain: True),
        ({**official_page, "html": '<a href="mailto:info@other.org">Contact</a>',
          "text": "Contact info@other.org"}, lambda _domain: True),
        (official_page, lambda _domain: False),
    ]:
        result = verify_contacts(
            Step3Outcome(),
            [{"org": org, "pages": [page]}],
            mx_lookup=mx_lookup,
            profile_fetch_fn=lambda _url: None,
        )
        assert result.verified == []


def test_company_level_public_provider_inbox_requires_official_publication_and_mx():
    org = make_org()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Contact our team at info@gmail.com",
        "html": '<a href="mailto:info@gmail.com">Contact</a>',
    }
    mx_calls = []
    result = verify_contacts(
        Step3Outcome(),
        [{"org": org, "pages": [page]}],
        mx_lookup=lambda domain: mx_calls.append(domain) or domain == "gmail.com",
        profile_fetch_fn=lambda _url: None,
    )
    assert len(result.verified) == 1
    contact = result.verified[0].contact
    assert contact.channel == "info@gmail.com"
    assert "published_public_email_provider" in contact.checks_passed
    assert "same_org_domain" not in contact.checks_passed
    assert mx_calls == ["gmail.com"]


def test_public_provider_personal_looking_email_is_not_labeled_general():
    org = make_org()
    page = {
        "url": "https://acmesolar.in/contact",
        "status": "ok",
        "text": "Contact Ravi at ravi.kumar@gmail.com",
        "html": '<a href="mailto:ravi.kumar@gmail.com">Ravi</a>',
    }
    result = verify_contacts(
        Step3Outcome(),
        [{"org": org, "pages": [page]}],
        mx_lookup=lambda _domain: (_ for _ in ()).throw(AssertionError("personal external address is not a shared inbox")),
        profile_fetch_fn=lambda _url: None,
    )
    assert result.verified == []


def test_email_name_variants_are_exact_after_normalization():
    assert email_matches_person("a.banerjee@acmesolar.in", "Dr. Asha Banerjee")
    assert email_matches_person("banerjee.asha@acmesolar.in", "Asha Banerjee")
    assert not email_matches_person("asha.sen@acmesolar.in", "Asha Banerjee")


if __name__ == "__main__":
    test_email_extractors_cover_mailto_obfuscation_and_cloudflare()
    test_exact_person_email_requires_official_domain_name_match_and_mx()
    test_generic_or_foreign_email_is_rejected_before_profile_fallback()
    test_email_without_mx_is_not_marked_verified()
    test_profile_fallback_rejects_non_200_responses()
    test_pattern_derived_email_requires_two_same_domain_examples_and_mx()
    test_wayback_only_emails_and_unmatched_contacts_are_not_verified()
    test_public_generic_contact_is_retained_for_review_without_a_person()
    test_email_name_variants_are_exact_after_normalization()
    print("Step 4 offline tests passed; no DNS, API, or live HTTP calls were made.")
