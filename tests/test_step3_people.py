"""Offline Step 3 tests. Every model and search call is injected and fake."""

import json

from agent.schemas import OrgCandidate
from agent.step3_people import (
    PeopleOut,
    PersonOut,
    build_evidence_windows,
    identify_people,
)


def org(name="Acme Solar", domain="acmesolar.in", category="Sponsor"):
    return OrgCandidate(
        org_name=name,
        category=category,
        city="Kolkata",
        domain=domain,
        homepage=f"https://{domain}/",
        discovered_via="offline-test",
    )


def record(organization, pages):
    return {"org": organization, "pages": pages}


def prompt_data(prompt):
    return json.loads(prompt.split("INPUT_DATA_JSON:\n", 1)[1])


def test_windows_are_role_centered_merged_and_capped():
    page = {
        "url": "https://acmesolar.in/team",
        "page_type": "team",
        "status": "ok",
        "text": "x" * 1200 + " Head of CSR " + "y" * 1600 + " CSR Manager " + "z" * 1200,
    }
    windows = build_evidence_windows([page], ["Head of CSR", "CSR Manager"], max_chars=900)
    assert windows
    assert all(window.page_url == page["url"] for window in windows)
    assert "Head of CSR" in windows[0].text
    assert sum(len(window.text) + len(window.page_url) + 32 for window in windows) <= 900


def test_primary_candidates_are_grounded_ranked_and_url_checked():
    team_url = "https://acmesolar.in/team"
    about_url = "https://acmesolar.in/about"
    far_url = "https://acmesolar.in/history"
    desk_url = "https://acmesolar.in/contact"
    far_text = "Ravi Sen\n" + ("unrelated archive words " * 18) + "Head of CSR"
    pages = [
        {
            "url": about_url,
            "page_type": "about",
            "status": "ok",
            "text": "Mohan Dutta is a former employee. " + ("archive note " * 30) + "Current Brand Manager: Mohan Dutta. Acme Solar, 2026.",
        },
        {
            "url": far_url,
            "page_type": "other",
            "status": "ok",
            "text": far_text,
        },
        {
            "url": team_url,
            "page_type": "team",
            "status": "ok",
            "text": "Leadership team, 2026. Asha Banerjee — Head of CSR at Acme Solar.",
        },
        {
            "url": desk_url,
            "page_type": "contact",
            "status": "ok",
            "text": "Info Desk — Head of CSR. Contact the office for help.",
        },
    ]
    calls = []

    def fake_ask(schema, prompt, **kwargs):
        calls.append((schema, prompt, kwargs))
        org_id = prompt_data(prompt)[0]["org_id"]
        return PeopleOut(people=[
            PersonOut(org_id=org_id, full_name="Mohan Dutta", role="Brand Manager", page_url=about_url),
            PersonOut(org_id=org_id, full_name="Asha Banerjee", role="Head of CSR", page_url=team_url),
            PersonOut(org_id=org_id, full_name="Ravi Sen", role="Head of CSR", page_url=far_url),
            PersonOut(org_id=org_id, full_name="Info Desk", role="Head of CSR", page_url=desk_url),
            PersonOut(org_id=org_id, full_name="Invented Person", role="Head of CSR", page_url=team_url),
            PersonOut(org_id=org_id, full_name="Asha Banerjee", role="Head of CSR", page_url="https://evil.example/team"),
        ])

    def unexpected_search(_query):
        raise AssertionError("fallback search should not run after a grounded person is found")

    result = identify_people(
        [record(org(), pages)],
        ask_fn=fake_ask,
        search_fn=unexpected_search,
        batch_orgs=3,
    )
    assert len(calls) == 1
    assert calls[0][2]["tier"] == "smart"
    assert calls[0][2]["tag"] == "s3_people"
    assert len(result.identified) == 1
    identified = result.identified[0]
    assert identified.person.full_name == "Asha Banerjee"
    assert str(identified.person.source_url) == team_url
    assert [candidate.full_name for candidate in identified.all_candidates] == ["Asha Banerjee", "Mohan Dutta"]
    assert result.dropped == []
    assert result.model_batches == 1


def test_batching_limits_primary_model_calls():
    inputs = []
    urls_by_id = {}
    names_by_id = {}
    for index in range(4):
        domain = f"company{index}.in"
        person_name = f"Person{index} Kumar"
        page_url = f"https://{domain}/team"
        inputs.append(record(org(f"Company {index}", domain), [{
            "url": page_url,
            "page_type": "team",
            "status": "ok",
            "text": f"{person_name} — Head of CSR at Company {index}.",
        }]))
        urls_by_id[f"org_{index:04d}"] = page_url
        names_by_id[f"org_{index:04d}"] = person_name
    calls = []

    def fake_ask(schema, prompt, **kwargs):
        calls.append(prompt)
        people = []
        for block in prompt_data(prompt):
            org_id = block["org_id"]
            people.append(PersonOut(
                org_id=org_id,
                full_name=names_by_id[org_id],
                role="Head of CSR",
                page_url=urls_by_id[org_id],
            ))
        return PeopleOut(people=people)

    result = identify_people(inputs, ask_fn=fake_ask, search_fn=lambda _q: [], batch_orgs=3)
    assert len(calls) == 2
    assert result.model_batches == 2
    assert len(result.identified) == 4


def test_snippet_fallback_is_bounded_and_grounded():
    orgs = [org("Alpha Solar", "alphasolar.in"), org("Beta Solar", "betasolar.in")]
    inputs = [
        record(item, [{
            "url": item.homepage,
            "page_type": "other",
            "status": "ok",
            "text": f"Welcome to {item.org_name}.",
        }])
        for item in orgs
    ]
    search_queries = []
    model_prompts = []

    def fake_search(query):
        search_queries.append(query)
        if "alphasolar.in" in query:
            return [{
                "title": "Anita Roy - Head of CSR | Alpha Solar",
                "url": "https://alphasolar.in/team",
                "snippet": "Anita Roy is Head of CSR at Alpha Solar, Kolkata.",
            }]
        return [{
            "title": "Directory listing for Beta Solar",
            "url": "https://zaubacorp.com/beta-solar",
            "snippet": "A made-up contact",
        }]

    def fake_ask(schema, prompt, **kwargs):
        model_prompts.append(prompt)
        org_id = prompt_data(prompt)[0]["org_id"]
        return PeopleOut(people=[PersonOut(
            org_id=org_id,
            full_name="Anita Roy",
            role="Head of CSR",
            page_url="https://alphasolar.in/team",
        )])

    result = identify_people(
        inputs,
        ask_fn=fake_ask,
        search_fn=fake_search,
        batch_orgs=3,
        max_snippet_fallbacks=1,
    )
    assert len(search_queries) == 1
    assert len(model_prompts) == 1
    assert result.fallback_searches == 1
    assert [item.person.full_name for item in result.identified] == ["Anita Roy"]
    assert len(result.dropped) == 1
    assert result.dropped[0]["domain"] == "betasolar.in"
    assert "fallback cap" in result.dropped[0]["reason"]


def test_non_official_or_placeholder_pages_are_not_sent_to_model():
    valid = org()
    pages = [
        {
            "url": "https://social.example/acme",
            "page_type": "team",
            "status": "ok",
            "text": "Asha Banerjee — Head of CSR",
        },
        {
            "url": "https://acmesolar.in/old-team",
            "page_type": "team",
            "status": "ok",
            "reason": "placeholder text detected",
            "text": "Asha Banerjee — Head of CSR",
        },
        {
            "url": "https://web.archive.org/web/20260101/https://acmesolar.in/team",
            "page_type": "team",
            "status": "wayback",
            "text": "Asha Banerjee — Head of CSR at Acme Solar.",
        },
    ]
    captured = []

    def fake_ask(schema, prompt, **kwargs):
        captured.append(prompt)
        org_id = prompt_data(prompt)[0]["org_id"]
        archive_url = "https://web.archive.org/web/20260101/https://acmesolar.in/team"
        return PeopleOut(people=[PersonOut(
            org_id=org_id,
            full_name="Asha Banerjee",
            role="Head of CSR",
            page_url=archive_url,
        )])

    result = identify_people(
        [record(valid, pages)],
        ask_fn=fake_ask,
        search_fn=lambda _q: [],
        max_snippet_fallbacks=0,
    )
    assert len(captured) == 1
    assert "social.example" not in captured[0]
    assert "placeholder text detected" not in captured[0]
    assert len(result.identified) == 1
    assert result.identified[0].person.role_score < 550  # A current team page would receive the non-Wayback bonus.


def test_college_delegation_roles_use_their_own_priority_list():
    organization = org("Kolkata Robotics Society", "kolkatarobotics.in", "College Delegation")
    page_url = "https://kolkatarobotics.in/society"
    page = {
        "url": page_url,
        "page_type": "about",
        "status": "ok",
        "text": "Riya Sen, Secretary of the Robotics Society, coordinates student events.",
    }

    def fake_ask(schema, prompt, **kwargs):
        org_id = prompt_data(prompt)[0]["org_id"]
        return PeopleOut(people=[PersonOut(
            org_id=org_id,
            full_name="Riya Sen",
            role="Secretary",
            page_url=page_url,
        )])

    result = identify_people(
        [record(organization, [page])],
        ask_fn=fake_ask,
        search_fn=lambda _q: [],
        max_snippet_fallbacks=0,
    )
    assert len(result.identified) == 1
    assert result.identified[0].person.full_name == "Riya Sen"
    assert result.identified[0].person.role_score > 300


if __name__ == "__main__":
    test_windows_are_role_centered_merged_and_capped()
    test_primary_candidates_are_grounded_ranked_and_url_checked()
    test_batching_limits_primary_model_calls()
    test_snippet_fallback_is_bounded_and_grounded()
    test_non_official_or_placeholder_pages_are_not_sent_to_model()
    test_college_delegation_roles_use_their_own_priority_list()
    print("Step 3 offline tests passed; no API or live search calls were made.")
