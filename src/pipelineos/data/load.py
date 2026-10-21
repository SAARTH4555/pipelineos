"""Load golden records into HubSpot: companies by ``company_key``, contacts by ``email``.

Companies go first so contacts can be associated to them in the same run. Every batch is at most
100 inputs with no repeated key (HubSpot rejects both), so inputs are coalesced per key before
batching. Per-row failures from a 207 response are collected, not raised, so one bad row does
not stop the other 99. Cost is O(n) work and ceil(n/100) upsert calls per object, plus
ceil(pairs/2000) association calls.

Fields HubSpot models differently are not sent: its ``industry`` is a fixed picklist, so the
import's free-text industry stays out until it is mapped.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pipelineos.data.clean import read_csv
from pipelineos.hubspot.client import (
    MAX_ASSOCIATION_BATCH,
    MAX_UPSERT_BATCH,
    AssociationInput,
    BatchError,
    HubSpotClient,
    HubSpotError,
    UpsertInput,
)

SEGMENT_THRESHOLDS = ((500, "enterprise"), (50, "mid_market"), (0, "smb"))


def batched[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def coalesce(inputs: Sequence[UpsertInput]) -> list[UpsertInput]:
    """One input per key, later values winning, first-seen order kept."""
    merged: dict[str, dict[str, str]] = {}
    for item in inputs:
        merged.setdefault(item.key, {}).update(item.properties)
    return [UpsertInput(key, props) for key, props in merged.items()]


def account_segment(employees: str) -> str:
    if not employees.isdigit():
        return ""
    count = int(employees)
    return next(segment for floor, segment in SEGMENT_THRESHOLDS if count >= floor)


def _non_empty(props: dict[str, str]) -> dict[str, str]:
    # An empty string would clear a value a rep may have filled in by hand.
    return {k: v for k, v in props.items() if v}


COMPANY_BASE_FIELDS = frozenset({"company_key", "name", "domain", "numberofemployees"})


def company_inputs(
    rows: list[dict[str, str]], extra_properties: frozenset[str] = frozenset()
) -> list[UpsertInput]:
    """Golden company rows to upserts.

    ``extra_properties`` are company properties the portal spec declares: any CSV column with one
    of those names is passed through (an upstream tool's ``signal_*`` columns, for instance).
    Columns the spec does not declare are never sent, so an unknown column cannot fail a load.
    """
    passthrough = extra_properties - COMPANY_BASE_FIELDS
    return coalesce(
        [
            UpsertInput(
                row["company_key"],
                _non_empty(
                    {
                        "name": row.get("name", ""),
                        "domain": row.get("domain", ""),
                        "numberofemployees": row.get("numberofemployees", ""),
                        "account_segment": account_segment(row.get("numberofemployees", "")),
                    }
                    | {name: row.get(name, "") for name in sorted(passthrough) if name in row}
                ),
            )
            for row in rows
            if row.get("company_key")
        ]
    )


def contact_inputs(rows: list[dict[str, str]]) -> list[UpsertInput]:
    return coalesce(
        [
            UpsertInput(
                row["email"],
                _non_empty(
                    {
                        "firstname": row.get("firstname", ""),
                        "lastname": row.get("lastname", ""),
                        "phone": row.get("phone", ""),
                        "jobtitle": row.get("jobtitle", ""),
                        "company": row.get("company_name", ""),
                        "signup_source": row.get("signup_source", ""),
                    }
                ),
            )
            for row in rows
            if row.get("email")
        ]
    )


@dataclass
class ObjectLoad:
    created: int = 0
    updated: int = 0
    batches: int = 0
    errors: list[BatchError] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)  # key -> HubSpot record id

    def summary(self) -> dict[str, object]:
        return {
            "created": self.created,
            "updated": self.updated,
            "failed": len(self.errors),
            "batches": self.batches,
            "first_errors": [
                {"key": e.key, "category": e.category, "message": e.message}
                for e in self.errors[:5]
            ],
        }


def upsert_all(
    client: HubSpotClient, object_type: str, id_property: str, inputs: Sequence[UpsertInput]
) -> ObjectLoad:
    load = ObjectLoad()
    for batch in batched(inputs, MAX_UPSERT_BATCH):
        result = client.batch_upsert(object_type, id_property, batch)
        load.batches += 1
        load.created += result.created
        load.updated += result.updated
        load.errors += result.errors
        sent = {item.key.lower(): item.key for item in batch}
        for row in result.results:
            # HubSpot may echo the key back normalized (emails lower-cased).
            load.ids[sent.get(row.key.lower(), row.key)] = row.record_id
    return load


def default_association_type(client: HubSpotClient, from_type: str, to_type: str) -> int:
    """The unlabeled HubSpot-defined association type between two objects."""
    for row in client.list_association_labels(from_type, to_type):
        if row.get("category") == "HUBSPOT_DEFINED" and not row.get("label"):
            return int(row["typeId"])
    raise HubSpotError(0, "CONFIG", f"no default association type for {from_type}->{to_type}")


@dataclass
class LoadReport:
    companies: ObjectLoad
    contacts: ObjectLoad
    associations: int
    skipped_associations: int

    def summary(self) -> dict[str, object]:
        return {
            "companies": self.companies.summary(),
            "contacts": self.contacts.summary(),
            "associations": self.associations,
            "skipped_associations": self.skipped_associations,
        }


def preview(clean_dir: Path) -> dict[str, int]:
    companies = company_inputs(read_csv(clean_dir / "companies.csv"))
    contact_rows = read_csv(clean_dir / "contacts.csv")
    contacts = contact_inputs(contact_rows)
    with_company = sum(1 for r in contact_rows if r.get("company_key"))
    return {
        "companies": len(companies),
        "contacts": len(contacts),
        "upsert_calls": -(-len(companies) // MAX_UPSERT_BATCH)
        - (-len(contacts) // MAX_UPSERT_BATCH),
        "associations": with_company,
    }


def load(
    client: HubSpotClient, clean_dir: Path, company_properties: frozenset[str] = frozenset()
) -> LoadReport:
    """``company_properties`` = the company properties the portal spec declares (see
    ``company_inputs``); CSV columns with those names are loaded too."""
    company_rows = read_csv(clean_dir / "companies.csv")
    contact_rows = read_csv(clean_dir / "contacts.csv")
    companies = upsert_all(
        client, "companies", "company_key", company_inputs(company_rows, company_properties)
    )
    contacts = upsert_all(client, "contacts", "email", contact_inputs(contact_rows))

    pairs: list[AssociationInput] = []
    skipped = 0
    for row in contact_rows:
        contact_id = contacts.ids.get(row.get("email", ""))
        company_id = companies.ids.get(row.get("company_key", ""))
        if not row.get("company_key"):
            continue
        if contact_id is None or company_id is None:
            skipped += 1  # one side failed to load; its error is already in the report
            continue
        pairs.append(AssociationInput(contact_id, company_id))
    associated = 0
    if pairs:
        type_id = default_association_type(client, "contacts", "companies")
        for chunk in batched(pairs, MAX_ASSOCIATION_BATCH):
            associated += client.batch_associate(
                "contacts", "companies", type_id, "HUBSPOT_DEFINED", chunk
            )
    return LoadReport(companies, contacts, associated, skipped)
