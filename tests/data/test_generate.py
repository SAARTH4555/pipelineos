from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from pipelineos.data.generate import GenConfig, generate, write_dataset


def read(path: Path) -> list[dict[str, str]]:
    with path.open() as handle:
        return list(csv.DictReader(handle))


def test_same_seed_gives_byte_identical_files(tmp_path: Path) -> None:
    for name in ("a", "b"):
        data, _ = generate(GenConfig(n=400, seed=11))
        write_dataset(data, tmp_path / name)
    for file in ("contacts.csv", "companies.csv", "truth_contacts.csv", "truth_companies.csv"):
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes()
    other, _ = generate(GenConfig(n=400, seed=12))
    write_dataset(other, tmp_path / "c")
    assert (tmp_path / "c" / "contacts.csv").read_bytes() != (
        tmp_path / "a" / "contacts.csv"
    ).read_bytes()


def test_truth_lines_up_with_rows_and_counts_match_summary(tmp_path: Path) -> None:
    data, summary = generate(GenConfig(n=2000, dirty_fraction=0.25, seed=3))
    write_dataset(data, tmp_path)
    contacts = read(tmp_path / "contacts.csv")
    truth = read(tmp_path / "truth_contacts.csv")
    assert [r["row_id"] for r in contacts] == [t["row_id"] for t in truth]
    assert len({r["row_id"] for r in contacts}) == len(contacts) == summary["contact_rows"]

    real = [t for t in truth if not t["entity_id"].startswith("junk")]
    per_entity = Counter(t["entity_id"] for t in real)
    assert len(per_entity) == 2000
    assert sum(per_entity.values()) - 2000 == summary["duplicate_rows"]

    tags = [d for t in truth for d in t["defects"].split(";")]
    by_kind = Counter(d.removeprefix("dup_") for d in tags if d.startswith("dup_"))
    assert dict(by_kind) == summary["duplicates_by_kind"]
    assert set(by_kind) == {
        "exact",
        "email_variant",
        "gmail_alias",
        "alt_work_email",
        "personal_email",
    }


def test_duplicate_kinds_look_like_their_names() -> None:
    data, _ = generate(GenConfig(n=1500, dirty_fraction=0.3, seed=5))
    rows = {r["row_id"]: r for r in data.contacts}
    first_row: dict[str, dict[str, str]] = {}
    for t in data.contact_truth:
        first_row.setdefault(t["entity_id"], rows[t["row_id"]])
    seen: dict[str, int] = defaultdict(int)
    for t in data.contact_truth:
        row, original = rows[t["row_id"]], first_row[t["entity_id"]]
        kinds = {d for d in t["defects"].split(";") if d.startswith("dup_")}
        if "dup_gmail_alias" in kinds:
            email = row["email"].strip().lower()
            assert "+" in email and email.endswith("@gmail.com")
        if "dup_personal_email" in kinds:
            assert row["email"].strip().lower() != original["email"].strip().lower()
            assert row["phone"]  # the shared phone is what links them
        if "dup_alt_work_email" in kinds:
            assert row["email"].strip().lower() != original["email"].strip().lower()
        for kind in kinds:
            seen[kind] += 1
    assert all(count > 0 for count in seen.values())


def test_clean_config_has_no_duplicates_or_noise() -> None:
    data, summary = generate(GenConfig(n=300, dirty_fraction=0.0, seed=1))
    assert summary["duplicate_rows"] == 0
    defects = set(summary["rows_with_defect"])
    assert defects <= {"freemail"}
    assert len(data.companies) == len({t["entity_id"] for t in data.company_truth})
    for row in data.contacts:
        assert row["email"] == row["email"].strip().lower()
        assert not row["phone"] or row["phone"].startswith("+1")


def test_company_rows_include_variants_of_the_same_company() -> None:
    data, summary = generate(GenConfig(n=800, dirty_fraction=0.4, seed=9))
    assert summary["company_rows"] > summary["companies"]
    rows = {r["row_id"]: r for r in data.companies}
    groups: dict[str, list[str]] = defaultdict(list)
    for t in data.company_truth:
        groups[t["entity_id"]].append(rows[t["row_id"]]["name"])
    assert any(len(set(names)) > 1 for names in groups.values())


def test_junk_rows_are_labelled() -> None:
    data, summary = generate(GenConfig(n=2000, dirty_fraction=0.5, seed=2))
    junk = [t for t in data.contact_truth if t["entity_id"].startswith("junk")]
    assert junk and all(t["defects"] == "invalid_email" for t in junk)
    rows = {r["row_id"]: r for r in data.contacts}
    assert all(
        "@" not in rows[t["row_id"]]["email"]
        or rows[t["row_id"]]["email"].endswith("@")
        or "." not in rows[t["row_id"]]["email"].split("@")[1]
        for t in junk
    )
    assert summary["rows_with_defect"]["invalid_email"] == len(junk)
