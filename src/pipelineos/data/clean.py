"""The cleanup pipeline: raw import in, golden records plus an audit trail and a report out."""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path

from pipelineos.data.dedupe import CompanyResult, ContactResult, dedupe_companies, dedupe_contacts
from pipelineos.data.quality import (
    GOLDEN_COLUMNS,
    RAW_COLUMNS,
    evaluate_companies,
    evaluate_contacts,
    quality_score,
)

GOLDEN_CONTACT_FIELDS = (
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
GOLDEN_COMPANY_FIELDS = (
    "company_key",
    "name",
    "domain",
    "numberofemployees",
    "industry",
    "merged_rows",
)


@dataclass
class CleanOutput:
    contacts: ContactResult
    companies: CompanyResult
    report: dict[str, object]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def clean(raw_dir: Path) -> CleanOutput:
    raw_contacts = read_csv(raw_dir / "contacts.csv")
    raw_companies = read_csv(raw_dir / "companies.csv")

    started = time.perf_counter()
    contacts = dedupe_contacts(raw_contacts)
    companies = dedupe_companies(raw_companies, contacts.golden)
    elapsed = time.perf_counter() - started

    stats = contacts.stats
    report: dict[str, object] = {
        "contacts": {
            "rows_in": len(raw_contacts),
            "rejected": len(contacts.rejected),
            "golden_out": len(contacts.golden),
            "merged_away": len(raw_contacts) - len(contacts.rejected) - len(contacts.golden),
            "merged_by": dict(sorted(stats.merged_by.items())),
        },
        "companies": {
            "rows_in": len(raw_companies),
            "rejected": len(companies.rejected),
            "golden_out": len(companies.golden),
        },
        "quality_before": quality_score(raw_contacts, RAW_COLUMNS),
        "quality_after": quality_score(contacts.golden, GOLDEN_COLUMNS),
        "matching": {
            "blocks": stats.blocks,
            "largest_block": stats.largest_block,
            "comparisons": stats.comparisons,
            "naive_comparisons": stats.rows * (stats.rows - 1) // 2,
            "seconds": round(elapsed, 3),
        },
    }
    truth_contacts = raw_dir / "truth_contacts.csv"
    if truth_contacts.exists():
        report["accuracy_contacts"] = evaluate_contacts(
            contacts.cluster_of, (r.row_id for r in contacts.rejected), read_csv(truth_contacts)
        )
    truth_companies = raw_dir / "truth_companies.csv"
    if truth_companies.exists():
        report["accuracy_companies"] = evaluate_companies(
            companies.cluster_of, read_csv(truth_companies)
        )
    return CleanOutput(contacts, companies, report)


def write_clean(output: CleanOutput, out_dir: Path) -> None:
    write_csv(out_dir / "contacts.csv", GOLDEN_CONTACT_FIELDS, output.contacts.golden)
    write_csv(out_dir / "companies.csv", GOLDEN_COMPANY_FIELDS, output.companies.golden)
    rejected = [
        {"kind": kind, "row_id": r.row_id, "reason": r.reason}
        for kind, rows in (
            ("contact", output.contacts.rejected),
            ("company", output.companies.rejected),
        )
        for r in rows
    ]
    write_csv(out_dir / "rejected.csv", ("kind", "row_id", "reason"), rejected)
    merges = [
        {"row_id": row_id, "survivor": survivor}
        for row_id, survivor in sorted(output.contacts.cluster_of.items())
        if row_id != survivor
    ]
    write_csv(out_dir / "merges.csv", ("row_id", "survivor"), merges)
