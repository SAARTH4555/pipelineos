from __future__ import annotations

from pathlib import Path

import pytest

from pipelineos.data.clean import write_csv
from pipelineos.data.load import (
    account_segment,
    batched,
    coalesce,
    company_inputs,
    contact_inputs,
    default_association_type,
    load,
    preview,
)
from pipelineos.hubspot.client import HubSpotError, UpsertInput
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import build_plan
from pipelineos.spec import PortalSpec

COMPANY_FIELDS = ("company_key", "name", "domain", "numberofemployees", "industry", "merged_rows")
CONTACT_FIELDS = (
    "contact_id",
    "email",
    "firstname",
    "lastname",
    "phone",
    "jobtitle",
    "company_key",
    "company_name",
    "signup_source",
    "created_at",
    "other_emails",
    "merged_rows",
)


def configured(spec: PortalSpec) -> FakeHubSpot:
    hs = FakeHubSpot()
    live = read_live(hs, spec)
    assert apply_plan(hs, build_plan(spec, live), live).ok
    return hs


def write_clean_dir(
    tmp_path: Path, companies: list[dict[str, str]], contacts: list[dict[str, str]]
) -> Path:
    write_csv(tmp_path / "companies.csv", COMPANY_FIELDS, companies)
    write_csv(tmp_path / "contacts.csv", CONTACT_FIELDS, contacts)
    return tmp_path


def company(key: str, employees: str = "40") -> dict[str, str]:
    return {
        "company_key": key,
        "name": key.split(".")[0].title(),
        "domain": key,
        "numberofemployees": employees,
        "industry": "devtools",
        "merged_rows": "",
    }


def contact(email: str, company_key: str = "", source: str = "organic") -> dict[str, str]:
    return {
        "contact_id": email,
        "email": email,
        "firstname": "Ana",
        "lastname": "Diaz",
        "phone": "",
        "jobtitle": "CTO",
        "company_key": company_key,
        "company_name": "",
        "signup_source": source,
        "created_at": "2025-01-01",
        "other_emails": "",
        "merged_rows": "",
    }


def test_helpers() -> None:
    assert [list(b) for b in batched([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    merged = coalesce(
        [UpsertInput("a", {"x": "1"}), UpsertInput("b", {}), UpsertInput("a", {"x": "2", "y": "3"})]
    )
    assert merged == [UpsertInput("a", {"x": "2", "y": "3"}), UpsertInput("b", {})]
    assert [account_segment(v) for v in ("12", "50", "499", "500", "", "n/a")] == [
        "smb",
        "mid_market",
        "mid_market",
        "enterprise",
        "",
        "",
    ]


def test_inputs_skip_blanks_and_never_send_free_text_industry() -> None:
    (only,) = company_inputs([company("acme.io", employees=""), {"company_key": ""}])
    assert only.properties == {"name": "Acme", "domain": "acme.io"}
    (person,) = contact_inputs([contact("ana@acme.io"), {"email": ""}])
    assert "phone" not in person.properties and person.properties["jobtitle"] == "CTO"


def test_load_creates_then_updates_and_associates(spec: PortalSpec, tmp_path: Path) -> None:
    hs = configured(spec)
    clean_dir = write_clean_dir(
        tmp_path,
        [company(f"c{i}.io", employees=str(10 * i)) for i in range(150)],
        [contact(f"p{i}@c{i % 150}.io", f"c{i % 150}.io") for i in range(250)]
        + [contact("solo@gmail.com")],
    )
    first = load(hs, clean_dir)
    assert (first.companies.created, first.contacts.created) == (150, 251)
    assert (first.companies.batches, first.contacts.batches) == (2, 3)
    assert first.associations == 250 and first.skipped_associations == 0
    # the fake rejects batches over 100, so 5 accepted calls means every batch was in bounds
    assert sum(1 for op, _ in hs.calls if op == "upsert") == 5
    mid = hs.records["companies"][first.companies.ids["c20.io"]]  # 200 employees
    big = hs.records["companies"][first.companies.ids["c60.io"]]  # 600 employees
    assert (mid["account_segment"], big["account_segment"]) == ("mid_market", "enterprise")
    assert "industry" not in mid

    again = load(hs, clean_dir)
    assert (again.companies.created, again.companies.updated) == (0, 150)
    assert (again.contacts.created, again.contacts.updated) == (0, 251)
    assert len(hs.records["contacts"]) == 251


def test_bad_rows_fail_alone_and_skip_their_association(spec: PortalSpec, tmp_path: Path) -> None:
    hs = configured(spec)
    clean_dir = write_clean_dir(
        tmp_path,
        [company("acme.io")],
        [contact("ana@acme.io", "acme.io"), contact("bo@acme.io", "acme.io", source="tiktok")],
    )
    report = load(hs, clean_dir)
    assert report.contacts.created == 1
    assert [e.key for e in report.contacts.errors] == ["bo@acme.io"]
    assert report.associations == 1 and report.skipped_associations == 1
    summary = report.summary()
    assert summary["contacts"]["failed"] == 1  # type: ignore[index]
    assert "not a valid option" in summary["contacts"]["first_errors"][0]["message"]  # type: ignore[index]


def test_unconfigured_portal_is_refused(tmp_path: Path) -> None:
    clean_dir = write_clean_dir(tmp_path, [company("acme.io")], [])
    with pytest.raises(HubSpotError, match="company_key is not a unique-value property"):
        load(FakeHubSpot(), clean_dir)


def test_default_association_type_lookup() -> None:
    hs = FakeHubSpot()
    assert default_association_type(hs, "contacts", "companies") == 279
    hs._labels[("contacts", "companies")] = []
    with pytest.raises(HubSpotError, match="no default association type"):
        default_association_type(hs, "contacts", "companies")


def test_preview_counts_without_api(tmp_path: Path) -> None:
    clean_dir = write_clean_dir(
        tmp_path,
        [company(f"c{i}.io") for i in range(101)],
        [contact("ana@c1.io", "c1.io"), contact("bo@gmail.com")],
    )
    assert preview(clean_dir) == {
        "companies": 101,
        "contacts": 2,
        "upsert_calls": 3,
        "associations": 1,
    }
