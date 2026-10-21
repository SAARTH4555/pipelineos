from __future__ import annotations

from pathlib import Path

from pipelineos.data.clean import clean, write_clean
from pipelineos.data.generate import GenConfig, generate, write_dataset
from pipelineos.data.quality import (
    GOLDEN_COLUMNS,
    RAW_COLUMNS,
    evaluate_companies,
    evaluate_contacts,
    pairwise,
    quality_score,
)


def test_pairwise_counts() -> None:
    truth = {"a": "1", "b": "1", "c": "1", "d": "2"}
    perfect = pairwise({"a": "x", "b": "x", "c": "x", "d": "y"}, truth)
    assert (perfect["precision"], perfect["recall"]) == (1.0, 1.0)
    split = pairwise({"a": "x", "b": "x", "c": "z", "d": "y"}, truth)
    assert split["precision"] == 1.0 and split["recall"] == round(1 / 3, 4)
    lumped = pairwise({"a": "x", "b": "x", "c": "x", "d": "x"}, truth)
    assert lumped["precision"] == 0.5 and lumped["recall"] == 1.0
    nothing = pairwise({"a": "x"}, {"a": "1"})
    assert nothing["f1"] == 1.0 and nothing["true_pairs"] == 0


def test_quality_score_components() -> None:
    rows = [
        {
            "email": "ana@acme.io",
            "first_name": "Ana",
            "last_name": "Diaz",
            "phone": "+14155550101",
            "company_website": "acme.io",
        },
        {
            "email": " ANA@acme.io",
            "first_name": "ANA",
            "last_name": "Diaz",
            "phone": "",
            "company_website": "",
        },
        {
            "email": "n/a",
            "first_name": "",
            "last_name": "",
            "phone": "415.555.0101",
            "company_website": "",
        },
        {
            "email": "bo@gmail.com",
            "first_name": "Bo",
            "last_name": "Chen",
            "phone": "",
            "company_website": "",
        },
    ]
    q = quality_score(rows, RAW_COLUMNS)
    assert q["validity"] == 0.75
    assert q["uniqueness"] == 0.25  # ana twice, n/a invalid, bo unique
    assert q["standardization"] == 0.5
    assert q["completeness"] == 0.5
    assert q["score"] == 50.0
    assert quality_score([], RAW_COLUMNS)["score"] == 0.0


def test_end_to_end_cleanup_against_ground_truth(tmp_path: Path) -> None:
    data, summary = generate(GenConfig(n=1500, dirty_fraction=0.25, seed=21))
    write_dataset(data, tmp_path / "raw")
    output = clean(tmp_path / "raw")
    write_clean(output, tmp_path / "clean")
    report = output.report

    accuracy = report["accuracy_contacts"]
    assert isinstance(accuracy, dict)
    assert accuracy["pairwise"]["recall"] >= 0.99
    assert accuracy["pairwise"]["precision"] >= 0.97
    assert all(v >= 0.95 for v in accuracy["recall_by_kind"].values())
    assert accuracy["junk"]["rejected"] == accuracy["junk"]["rows"]
    assert accuracy["junk"]["valid_rows_wrongly_rejected"] == 0
    assert accuracy["duplicate_rate_after"] < 0.01 < accuracy["duplicate_rate_before"]

    before, after = report["quality_before"], report["quality_after"]
    assert isinstance(before, dict) and isinstance(after, dict)
    assert after["score"] > before["score"]
    assert after["uniqueness"] == after["standardization"] == after["validity"] == 1.0

    matching = report["matching"]
    assert isinstance(matching, dict)
    assert matching["comparisons"] < matching["naive_comparisons"] / 1000

    companies = report["accuracy_companies"]
    assert isinstance(companies, dict)
    assert companies["key_accuracy"] >= 0.95

    for name in ("contacts.csv", "companies.csv", "rejected.csv", "merges.csv"):
        assert (tmp_path / "clean" / name).exists()
    assert summary["people"] == 1500


def test_golden_columns_score_cleaned_output() -> None:
    golden = [
        {
            "email": "ana@acme.io",
            "firstname": "Ana",
            "lastname": "Diaz",
            "phone": "",
            "company_key": "acme.io",
        }
    ]
    assert quality_score(golden, GOLDEN_COLUMNS)["score"] == 100.0


def test_evaluate_handles_empty_inputs() -> None:
    assert evaluate_contacts({}, [], [])["duplicate_rate_after"] == 0.0
    assert evaluate_companies({}, [])["key_accuracy"] == 0.0
