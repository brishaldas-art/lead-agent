"""Offline Step 5 tests; all Sheets behavior uses a fake client."""

import csv
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent.schemas import OrgCandidate
from agent.step3_people import Person
from agent.step4_contacts import Contact, Step4Outcome, VerifiedLead
from agent.step5_export import AUDIT_HEADERS, LEAD_ALL_HEADERS, MAIN_HEADERS, export_verified_leads


def sample_inputs():
    org = OrgCandidate(
        org_name="Acme Solar",
        category="Sponsor",
        city="Kolkata",
        domain="acmesolar.in",
        homepage="https://acmesolar.in/",
        discovered_via="offline-test",
    )
    person = Person(
        full_name="Asha Banerjee",
        role="Head of CSR",
        source_url="https://acmesolar.in/team",
        role_score=550,
    )
    contact = Contact(
        channel="asha.banerjee@acmesolar.in",
        channel_type="email",
        status="Verified",
        evidence_url="https://acmesolar.in/contact",
        supporting_urls=["https://acmesolar.in/contact"],
        checks_passed=["found_on_live_official_page", "person_name_match", "mx_ok"],
    )
    lead = VerifiedLead(org=org, person=person, contact=contact)
    records = [{
        "org": org,
        "verification": {"target_city": "Kolkata"},
        "pages": [
            {
                "url": "https://acmesolar.in/contact",
                "status": "ok",
                "text": "Acme Solar office address: Kolkata, West Bengal.",
                "html": "",
            },
        ],
    }]
    step4 = Step4Outcome(
        verified=[lead],
        observed_contacts=[{
            "organization_name": "Acme Solar",
            "root_domain": "acmesolar.in",
            "channel": "info@acmesolar.in",
            "channel_type": "email",
            "verification_status": "Observed—generic inbox; not person-verified",
            "evidence_urls": ["https://acmesolar.in/contact"],
            "checks_passed": ["published_on_live_official_domain_page"],
        }],
    )
    people = SimpleNamespace(
        identified=[SimpleNamespace(org=org, person=person, all_candidates=[person])],
        dropped=[],
    )
    return step4, records, people


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.values = []

    def get_all_values(self):
        return [list(row) for row in self.values]

    def update(self, values, range_name):
        assert range_name == "A1"
        self.values = [list(row) for row in values]

    def append_rows(self, rows, value_input_option):
        assert value_input_option == "RAW"
        self.values.extend([list(row) for row in rows])


class FakeSpreadsheet:
    url = "https://docs.google.com/spreadsheets/d/fake-sheet/edit"

    def __init__(self):
        self._worksheets = [FakeWorksheet("Sheet1")]

    def worksheets(self):
        return self._worksheets

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet(title)
        self._worksheets.append(worksheet)
        return worksheet


class FakeSheetsClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, key):
        assert key == "offline-sheet-id"
        return self.spreadsheet


def test_local_csv_sqlite_audit_and_deduplication_are_idempotent():
    outcome, records, people = sample_inputs()
    with tempfile.TemporaryDirectory() as temp_dir:
        output = Path(temp_dir)
        first = export_verified_leads(
            outcome,
            records,
            output_dir=output,
            people_result=people,
            sheet_id="",
            credentials_path="",
        )
        second = export_verified_leads(
            outcome,
            records,
            output_dir=output,
            people_result=people,
            sheet_id="",
            credentials_path="",
        )

        assert first.rows_added == 1
        assert first.total_rows == 1
        assert first.duplicates_skipped == 0
        assert first.review_candidates == 1
        assert second.review_candidates == 1
        assert first.run_id > 0 and second.run_id > first.run_id
        assert second.rows_added == 0
        assert second.total_rows == 1
        assert second.duplicates_skipped == 1
        assert first.sheets_status == "not_configured"
        assert Path(first.csv_path).is_file()
        assert Path(first.audit_csv_path).is_file()
        assert Path(first.lead_all_csv_path).is_file()
        assert Path(first.database_path).is_file()

        with Path(first.csv_path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert tuple(rows[0]) == MAIN_HEADERS
        assert len(rows) == 2
        assert rows[1] == [
            "Acme Solar", "Sponsor", "Kolkata", "Asha Banerjee, Head of CSR",
            "asha.banerjee@acmesolar.in", "Verified",
        ]

        with Path(first.audit_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            audit_rows = list(csv.reader(stream))
        assert tuple(audit_rows[0]) == AUDIT_HEADERS
        assert audit_rows[1][1] == "acmesolar.in"
        assert audit_rows[1][5] == "email"
        assert "mx_ok" in audit_rows[1][11]

        with Path(first.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            review_rows = list(csv.reader(stream))
        assert tuple(review_rows[0]) == LEAD_ALL_HEADERS
        assert len(review_rows) == 2
        assert review_rows[1] == [
            "Acme Solar", "Sponsor", "Kolkata", "Asha Banerjee, Head of CSR",
            "asha.banerjee@acmesolar.in", "Verified", "",
        ]

        with sqlite3.connect(first.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM candidate_review").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 2


def test_company_level_contact_is_exported_with_explicit_label():
    outcome, records, _people = sample_inputs()
    source_page = "https://acmesolar.in/contact"
    original = outcome.verified[0]
    general_person = Person(
        full_name="General Contact",
        role="Company Contact",
        source_url=source_page,
        role_score=0,
    )
    general_contact = Contact(
        channel="info@acmesolar.in",
        channel_type="email",
        status="Verified",
        evidence_url=source_page,
        supporting_urls=[source_page],
        checks_passed=[
            "published_on_live_official_page",
            "company_level_shared_inbox",
            "same_org_domain",
            "syntax_valid",
            "mx_ok",
        ],
    )
    outcome.verified = [VerifiedLead(org=original.org, person=general_person, contact=general_contact)]
    people = SimpleNamespace(identified=[], dropped=[])
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            outcome, records, output_dir=Path(temp_dir), sheet_id="",
            credentials_path="", people_result=people,
        )
        assert result.rows_added == 1
        with Path(result.csv_path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert rows[1] == [
            "Acme Solar", "Sponsor", "Kolkata", "General Contact, Company Contact",
            "info@acmesolar.in", "Verified",
        ]
        with Path(result.audit_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            audit_rows = list(csv.reader(stream))
        assert audit_rows[1][2:4] == ["General Contact", "Company Contact"]
        assert "company_level_shared_inbox" in audit_rows[1][11]

    # A verified shared inbox does not bypass Step 5's official-page city gate.
    records[0]["pages"][0]["text"] = "Acme Solar office in West Bengal."
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            outcome, records, output_dir=Path(temp_dir), sheet_id="",
            credentials_path="", people_result=people,
        )
        assert result.rows_added == 0
        assert len(result.dropped) == 1
        assert "not confirmed" in result.dropped[0]["reason"]
        with Path(result.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            review_rows = list(csv.reader(stream))
        assert review_rows[1][5] == "Contact Verified; Lead Not Exported"
        assert "organization city is not confirmed" in review_rows[1][6]


def test_city_alias_confirms_official_location():
    outcome, records, people = sample_inputs()
    records[0]["pages"][0]["text"] = "Registered office: Calcutta, West Bengal."
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            outcome, records, output_dir=Path(temp_dir), sheet_id="",
            credentials_path="", people_result=people,
            run_context={"category": "Sponsor", "target_location": "Kolkata"},
        )
        assert result.rows_added == 1
        with Path(result.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert rows[1][6] == ""


def test_unconfirmed_location_drops_row_and_does_not_call_sheets():
    outcome, records, people = sample_inputs()
    records[0]["pages"][0]["text"] = "Acme Solar office address: West Bengal."
    calls = []
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            outcome,
            records,
            output_dir=Path(temp_dir),
            people_result=people,
            sheet_id="",
            credentials_path="",
            sheets_client_factory=lambda _path: calls.append(_path),
        )
        assert result.rows_added == 0
        assert len(result.dropped) == 1
        assert "not confirmed" in result.dropped[0]["reason"]
        assert result.review_candidates == 1
        with sqlite3.connect(result.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM candidate_review").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 1
            reasons, contacts = connection.execute(
                "SELECT review_reasons_json, step4_contacts_json FROM candidate_review"
            ).fetchone()
            assert "Target location is not confirmed" in reasons
            assert "asha.banerjee@acmesolar.in" in contacts
        assert calls == []


def test_empty_run_still_records_a_run_in_sqlite():
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            Step4Outcome(),
            [],
            output_dir=Path(temp_dir),
            sheet_id="",
            credentials_path="",
            run_context={"category": "Sponsor", "target_location": "Kolkata"},
        )
        assert result.review_candidates == 0
        assert result.run_id == 1
        with Path(result.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            assert tuple(next(csv.reader(stream))) == LEAD_ALL_HEADERS
        with sqlite3.connect(result.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM candidate_review").fetchone()[0] == 0
            run = connection.execute(
                "SELECT category, target_location, discovered_candidates FROM pipeline_runs"
            ).fetchone()
            assert run == ("Sponsor", "Kolkata", 0)


def test_quota_fallback_candidate_is_not_exported_as_accepted_lead():
    step4, records, people = sample_inputs()
    original = step4.verified[0]
    review_org = original.org.model_copy(update={
        "relevance_verified": False,
        "classification_status": "llm_unavailable_needs_review",
    })
    review_lead = original.model_copy(update={"org": review_org})
    step4.verified = [review_lead]
    records[0]["org"] = review_org
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            step4, records, output_dir=Path(temp_dir), sheet_id="",
            credentials_path="", people_result=people,
        )
        assert result.rows_added == 0
        assert result.dropped[0]["reason"] == "organization relevance is awaiting human review"
        with Path(result.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) == 2
        assert rows[1][5] == "Contact Verified; Lead Not Exported"
        assert "organization relevance is awaiting human review" in rows[1][6]


def test_review_csv_keeps_candidate_with_no_pages_or_contact():
    org = OrgCandidate(
        org_name="Sample Systems",
        category="Sponsor",
        city=None,
        domain="samplesystems.in",
        homepage="https://samplesystems.in/",
        discovered_via="search",
    )
    record = {
        "org": org,
        "pages": [],
        "candidate_status": "no_page_available",
        "verification": {"status": "location_unverified", "target_city": "Kolkata"},
    }
    people = SimpleNamespace(identified=[], dropped=[{
        "org_name": "Sample Systems", "domain": "samplesystems.in", "reason": "no usable official-site pages",
    }])
    with tempfile.TemporaryDirectory() as temp_dir:
        result = export_verified_leads(
            Step4Outcome(), [record], output_dir=Path(temp_dir),
            sheet_id="", credentials_path="", people_result=people,
        )
        assert result.rows_added == 0
        with Path(result.lead_all_csv_path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) == 2
        assert rows[1][0] == "Sample Systems"
        assert rows[1][2] == "Kolkata"
        assert rows[1][3] == ""
        assert rows[1][4] == ""
        assert rows[1][5] == "Not Verified"
        assert "no usable official-site pages" in rows[1][6]
        assert "sponsorship" not in rows[1][6].casefold()
        with sqlite3.connect(result.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM candidate_review").fetchone()[0] == 1


def test_optional_google_sheet_sync_uses_fake_client_and_exact_headers():
    outcome, records, people = sample_inputs()
    spreadsheet = FakeSpreadsheet()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        credential_file = root / "credentials.json"
        credential_file.write_text("fake test fixture; never sent to Google", encoding="utf-8")

        def factory(path):
            assert path == str(credential_file)
            assert (root / "leads.csv").is_file()
            return FakeSheetsClient(spreadsheet)

        result = export_verified_leads(
            outcome,
            records,
            output_dir=root,
            people_result=people,
            sheet_id="offline-sheet-id",
            credentials_path=credential_file,
            sheets_client_factory=factory,
        )

        assert result.sheets_status == "synced"
        assert result.sheet_url == FakeSpreadsheet.url
        main_sheet, audit_sheet = spreadsheet.worksheets()
        assert tuple(main_sheet.values[0]) == MAIN_HEADERS
        assert len(main_sheet.values) == 2
        assert tuple(audit_sheet.values[0]) == AUDIT_HEADERS
        assert len(audit_sheet.values) == 2


if __name__ == "__main__":
    test_local_csv_sqlite_audit_and_deduplication_are_idempotent()
    test_city_alias_confirms_official_location()
    test_unconfirmed_location_drops_row_and_does_not_call_sheets()
    test_empty_run_still_records_a_run_in_sqlite()
    test_quota_fallback_candidate_is_not_exported_as_accepted_lead()
    test_review_csv_keeps_candidate_with_no_pages_or_contact()
    test_optional_google_sheet_sync_uses_fake_client_and_exact_headers()
    print("Step 5 offline tests passed; no Google API or network calls were made.")
