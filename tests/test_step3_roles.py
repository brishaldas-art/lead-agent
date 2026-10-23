"""Offline checks for Step 3 role configuration and deterministic guards."""

from agent.step3_people import (
    _find_name_spans,
    _load_roles,
    _match_target_role,
    _role_keywords,
)


def test_role_configuration_covers_both_lead_categories():
    roles = _load_roles()
    assert set(roles) == {"Sponsor", "College Delegation"}
    for category, entries in roles.items():
        assert [item["priority"] for item in entries] == [1, 2, 3, 4, 5]
        assert all(item["aliases"] and item["keywords"] for item in entries)
        assert len(_role_keywords(category, entries)) > 5


def test_role_matching_is_category_specific_and_rejects_unlisted_titles():
    sponsor = _match_target_role("CSR Manager", "Sponsor")
    college = _match_target_role("Society Secretary", "College Delegation")
    assert sponsor and sponsor[0]["priority"] == 1
    assert college and college[0]["priority"] == 2
    assert _match_target_role("Chief Executive Officer", "Sponsor") is None


def test_name_guard_rejects_generic_entries_but_accepts_groundable_names():
    assert _find_name_spans("Info Desk", "Info Desk — Head of CSR") == []
    assert _find_name_spans("Team Admin", "Team Admin — Head of CSR") == []
    assert _find_name_spans("Dr. Asha Banerjee", "Asha Banerjee — Head of CSR")


if __name__ == "__main__":
    test_role_configuration_covers_both_lead_categories()
    test_role_matching_is_category_specific_and_rejects_unlisted_titles()
    test_name_guard_rejects_generic_entries_but_accepts_groundable_names()
    print("Step 3 role tests passed; no API or live search calls were made.")
