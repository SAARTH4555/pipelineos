from __future__ import annotations

from pipelineos.data.dedupe import (
    UnionFind,
    dedupe_companies,
    dedupe_contacts,
    same_first_name,
    same_last_name,
)


def row(
    row_id: str, email: str, first: str = "Ana", last: str = "Diaz", **extra: str
) -> dict[str, str]:
    base = {
        "row_id": row_id,
        "email": email,
        "first_name": first,
        "last_name": last,
        "job_title": "",
        "phone": "",
        "company_name": "Acme",
        "company_website": "acme.io",
        "signup_source": "organic",
        "created_at": "2025-01-01",
    }
    return base | extra


def clusters(rows: list[dict[str, str]]) -> list[set[str]]:
    result = dedupe_contacts(rows)
    groups: dict[str, set[str]] = {}
    for row_id, cluster in result.cluster_of.items():
        groups.setdefault(cluster, set()).add(row_id)
    return sorted(groups.values(), key=lambda g: sorted(g))


def test_union_find() -> None:
    uf = UnionFind(5)
    assert uf.union(0, 1) and uf.union(3, 4) and uf.union(1, 4)
    assert not uf.union(0, 3)
    assert uf.find(0) == uf.find(4) and uf.find(2) == 2


def test_name_rules() -> None:
    assert same_first_name("Jon", "John")
    assert same_first_name("Chris", "Christopher")
    assert same_first_name("Katherine", "Katherin")  # typo
    assert not same_first_name("Al", "Alan")  # prefix too short
    assert not same_first_name("Maria", "Mark")
    assert not same_first_name("", "Ana")
    assert same_last_name("Johnson", "Jonson")  # one dropped letter, 92%
    # Conservative on purpose: below 90% similarity stays apart, even if a typo is possible.
    assert not same_last_name("Hansen", "Hanson")
    assert not same_last_name("Gonzalez", "Gonzales")
    assert not same_last_name("Smith", "Smyth-Jones")
    assert not same_last_name("", "")


def test_exact_and_alias_keys_merge() -> None:
    groups = clusters(
        [
            row("c1", "ana@acme.io"),
            row("c2", "  ANA@ACME.IO "),
            row("c3", "ana.diaz+ci@gmail.com", company_website=""),
            row("c4", "anadiaz@gmail.com", company_website=""),
        ]
    )
    assert groups == [{"c1", "c2"}, {"c3", "c4"}]


def test_phone_links_personal_and_work_email_only_with_same_last_name() -> None:
    groups = clusters(
        [
            row("c1", "ana@acme.io", phone="(415) 555-0101"),
            row("c2", "ana.d@gmail.com", phone="+1 415 555 0101", company_website=""),
            # front-desk number shared by a different person: not merged
            row("c3", "bo@acme.io", first="Bo", last="Chen", phone="415.555.0101"),
        ]
    )
    assert groups == [{"c1", "c2"}, {"c3"}]


def test_fuzzy_names_merge_inside_a_company_only() -> None:
    groups = clusters(
        [
            row("c1", "john.smith@acme.io", first="John", last="Smith"),
            row("c2", "jsmith@acme.io", first="Jon", last="smith"),
            row(
                "c3",
                "john.smith@other.dev",
                first="John",
                last="Smith",
                company_website="other.dev",
            ),
        ]
    )
    assert groups == [{"c1", "c2"}, {"c3"}]


def test_namesakes_with_different_phones_stay_apart() -> None:
    result = dedupe_contacts(
        [
            row("c1", "sam@acme.io", first="Samuel", last="Williams", phone="4155550101"),
            row("c2", "swilliams@acme.io", first="Samuel", last="Williams", phone="4155550199"),
        ]
    )
    assert len(result.golden) == 2
    assert result.stats.merged_by["vetoed_by_phone"] == 1


def test_invalid_rows_are_rejected_not_merged() -> None:
    result = dedupe_contacts([row("c1", "n/a"), row("c2", "test@test"), row("c3", "ana@acme.io")])
    assert [(r.row_id, r.reason) for r in result.rejected] == [
        ("c1", "invalid_email"),
        ("c2", "invalid_email"),
    ]
    assert len(result.golden) == 1


def test_golden_record_prefers_work_email_and_fills_gaps() -> None:
    result = dedupe_contacts(
        [
            row(
                "c1",
                "ana.diaz@gmail.com",
                first="ANA",
                phone="4155550101",
                company_website="",
                created_at="2025-01-02",
                signup_source="event",
            ),
            row(
                "c2",
                "ana@acme.io",
                phone="415-555-0101",
                job_title="CTO",
                created_at="2025-03-01",
                signup_source="organic",
            ),
        ]
    )
    (golden,) = result.golden
    assert golden["email"] == "ana@acme.io"  # work address wins over freemail
    assert golden["contact_id"] == "c2"
    assert golden["firstname"] == "Ana"
    assert golden["phone"] == "+14155550101"
    assert golden["jobtitle"] == "CTO"
    assert golden["company_key"] == "acme.io"
    assert golden["signup_source"] == "event"  # first touch
    assert golden["created_at"] == "2025-01-02"
    assert golden["other_emails"] == "ana.diaz@gmail.com"
    assert golden["merged_rows"] == "c1;c2"


def test_output_is_deterministic_regardless_of_row_order() -> None:
    rows = [
        row("c1", "ana@acme.io"),
        row("c2", "ANA@acme.io", job_title="CTO"),
        row("c3", "bo@acme.io", first="Bo", last="Chen"),
    ]
    assert dedupe_contacts(rows).golden == dedupe_contacts(list(reversed(rows))).golden


def test_companies_group_by_domain_and_resolve_by_name() -> None:
    companies = [
        {
            "row_id": "k1",
            "name": "Acme Inc.",
            "website": "https://www.acme.io/",
            "employees": "40",
            "industry": "devtools",
        },
        {
            "row_id": "k2",
            "name": "ACME, Inc",
            "website": "acme.io",
            "employees": "45",
            "industry": "devtools",
        },
        {"row_id": "k3", "name": "acme", "website": "", "employees": "", "industry": ""},
        {"row_id": "k4", "name": "Ghost Co", "website": "", "employees": "", "industry": ""},
    ]
    contacts = [
        {"company_key": "acme.io", "company_name": "Acme"},
        {"company_key": "beta.dev", "company_name": "Beta"},
        {"company_key": "", "company_name": ""},
    ]
    result = dedupe_companies(companies, contacts)
    by_key = {c["company_key"]: c for c in result.golden}
    assert by_key["acme.io"]["name"] == "Acme"
    assert by_key["acme.io"]["numberofemployees"] == "45"
    assert by_key["acme.io"]["merged_rows"] == "k1;k2;k3"
    assert by_key["beta.dev"]["name"] == "Beta"  # known only from a contact's work email
    assert [r.row_id for r in result.rejected] == ["k4"]
    assert result.cluster_of == {"k1": "acme.io", "k2": "acme.io", "k3": "acme.io"}
