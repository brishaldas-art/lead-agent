"""Offline CLI wiring test for Step 3; all external work is patched out."""

import io
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import main as app
from agent.schemas import OrgCandidate
from agent.step3_people import OrgWithPerson, Person, Step3Outcome
from agent.schemas import SearchPlan


def test_main_runs_step3_and_prints_grounded_person():
    plan = SearchPlan(
        source="prompt",
        category="Sponsor",
        city="Kolkata",
        industries=["solar"],
        queries=["solar companies Kolkata"],
    )
    organization = OrgCandidate(
        org_name="Acme Solar",
        category="Sponsor",
        city="Kolkata",
        domain="acmesolar.in",
        homepage="https://acmesolar.in/",
        discovered_via="offline-test",
    )
    records = [{"org": organization, "pages": [{"status": "ok", "url": organization.homepage, "text": ""}]}]
    person = Person(
        full_name="Asha Banerjee",
        role="Head of CSR",
        source_url="https://acmesolar.in/team",
        role_score=550,
    )
    outcome = Step3Outcome(identified=[OrgWithPerson(
        org=organization,
        person=person,
        all_candidates=[person],
    )], model_batches=1)
    output = io.StringIO()

    with patch.object(sys, "argv", ["main.py", "--prompt", "Find solar sponsors in Kolkata"]), \
         patch.object(app, "build_plan", return_value=plan), \
         patch.object(app, "run_discovery", return_value=records) as discovery, \
         patch("agent.step3_people.identify_people", return_value=outcome) as step3, \
         redirect_stdout(output):
        assert app.main() == 0

    rendered = output.getvalue()
    assert "Running Step 3: decision-maker identification" in rendered
    assert "Asha Banerjee | Head of CSR" in rendered
    assert "https://acmesolar.in/team" in rendered
    discovery.assert_called_once_with(plan)
    step3.assert_called_once_with(records)


if __name__ == "__main__":
    test_main_runs_step3_and_prints_grounded_person()
    print("Step 3 CLI integration test passed; no API or live search calls were made.")
