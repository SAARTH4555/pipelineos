"""Sync product usage into HubSpot: workspaces, their PQL roll-up on companies, members as contacts.

Order matters: companies and contacts first, then workspaces, then associations, so every
association has both record ids. Records whose properties hash the same as last run are not
sent at all (see ``state.py``), so a sync with no new usage makes zero write calls.

What sync deliberately does not write: lifecycle stage and lead status. Those belong to the
HubSpot workflow that reacts to ``is_pql`` (see the manual checklist), so a nightly job never
overwrites a status a rep set by hand.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from pipelineos.data.load import batched, default_association_type
from pipelineos.hubspot.client import (
    MAX_ASSOCIATION_BATCH,
    MAX_UPSERT_BATCH,
    AssociationInput,
    BatchError,
    HubSpotClient,
    HubSpotError,
    UpsertInput,
)
from pipelineos.usage.score import PqlResult, WorkspaceFacts, best_plan
from pipelineos.usage.state import SyncState

WORKSPACE_SCHEMA = "workspace"
ADMIN_LABEL, MEMBER_LABEL = "Admin", "Member"


@dataclass
class ObjectSync:
    to_send: int = 0
    skipped_unchanged: int = 0
    created: int = 0
    updated: int = 0
    errors: list[BatchError] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "to_send": self.to_send,
            "skipped_unchanged": self.skipped_unchanged,
            "created": self.created,
            "updated": self.updated,
            "failed": len(self.errors),
        }


@dataclass
class SyncReport:
    dry_run: bool
    workspaces: ObjectSync
    companies: ObjectSync
    contacts: ObjectSync
    associations_to_send: int = 0
    associations_created: int = 0
    associations_skipped_missing_record: int = 0
    pql_workspaces: int = 0
    pql_companies: int = 0

    @property
    def writes(self) -> int:
        parts = (self.workspaces, self.companies, self.contacts)
        return sum(p.to_send for p in parts) + self.associations_to_send

    def summary(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "workspaces": self.workspaces.summary(),
            "companies": self.companies.summary(),
            "contacts": self.contacts.summary(),
            "associations": {
                "to_send": self.associations_to_send,
                "created": self.associations_created,
                "skipped_missing_record": self.associations_skipped_missing_record,
            },
            "pql_workspaces": self.pql_workspaces,
            "pql_companies": self.pql_companies,
        }


def _non_empty(props: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in props.items() if v}


def workspace_properties(f: WorkspaceFacts, result: PqlResult) -> dict[str, str]:
    return _non_empty(
        {
            "workspace_name": f.name,
            "plan": f.plan,
            "seats": str(f.seats),
            "active_users_14d": str(f.active_users_14d),
            "key_actions_14d": str(f.key_actions_14d),
            "signup_date": f.signup_date.isoformat() if f.signup_date else "",
            "last_active_date": f.last_active_date.isoformat() if f.last_active_date else "",
            "pql_score": str(result.score),
            "is_pql": "true" if result.is_pql else "false",
            "pql_reasons": result.reasons_text,
        }
    )


def company_rollups(
    facts: dict[str, WorkspaceFacts],
    results: dict[str, PqlResult],
    state: SyncState,
    as_of: date,
) -> dict[str, dict[str, str]]:
    """One roll-up per company: PQL if any workspace is, best score and its reasons."""
    by_company: dict[str, list[str]] = defaultdict(list)
    for workspace_id, f in facts.items():
        if f.company_key:
            by_company[f.company_key].append(workspace_id)
    rollups: dict[str, dict[str, str]] = {}
    for key, workspace_ids in sorted(by_company.items()):
        top = max(workspace_ids, key=lambda w: (results[w].score, w))
        is_pql = any(results[w].is_pql for w in workspace_ids)
        if is_pql:
            state.pql_since.setdefault(key, as_of.isoformat())
        rollups[key] = _non_empty(
            {
                "is_pql": "true" if is_pql else "false",
                "pql_score": str(results[top].score),
                "pql_reasons": results[top].reasons_text,
                "pql_since": state.pql_since.get(key, ""),
                "workspace_count": str(len(workspace_ids)),
                "product_plan": best_plan([facts[w].plan for w in workspace_ids]),
            }
        )
    return rollups


def contact_properties(facts: dict[str, WorkspaceFacts]) -> dict[str, dict[str, str]]:
    contacts: dict[str, dict[str, str]] = {}
    for f in facts.values():
        for email, user_id in f.user_ids.items():
            contacts.setdefault(email, {"product_user_id": user_id})
    return dict(sorted(contacts.items()))


def _sync_object(
    client: HubSpotClient,
    state: SyncState,
    name: str,
    api_type: str,
    id_property: str,
    desired: dict[str, dict[str, str]],
    dry_run: bool,
) -> ObjectSync:
    out = ObjectSync()
    changed = [UpsertInput(k, p) for k, p in desired.items() if not state.unchanged(name, k, p)]
    out.to_send = len(changed)
    out.skipped_unchanged = len(desired) - len(changed)
    if dry_run:
        return out
    for batch in batched(changed, MAX_UPSERT_BATCH):
        result = client.batch_upsert(api_type, id_property, batch)
        by_key = {item.key.lower(): item for item in batch}
        for row in result.results:
            item = by_key[row.key.lower()]
            state.remember(name, item.key, dict(item.properties), row.record_id)
        out.created += result.created
        out.updated += result.updated
        out.errors += result.errors
    return out


def _label_type(client: HubSpotClient, from_type: str, to_type: str, label: str) -> int:
    for row in client.list_association_labels(from_type, to_type):
        if row.get("category") == "USER_DEFINED" and row.get("label") == label:
            return int(row["typeId"])
    raise HubSpotError(0, "CONFIG", f"association label {label!r} missing: run pipelineos apply")


def workspace_type(client: HubSpotClient) -> str:
    for schema in client.list_schemas():
        if schema.get("name") == WORKSPACE_SCHEMA:
            return str(schema["objectTypeId"])
    raise HubSpotError(0, "CONFIG", "custom object 'workspace' missing: run pipelineos apply")


def sync(
    client: HubSpotClient,
    facts: dict[str, WorkspaceFacts],
    results: dict[str, PqlResult],
    state: SyncState,
    as_of: date,
    *,
    dry_run: bool,
) -> SyncReport:
    ws_type = workspace_type(client)
    companies = company_rollups(facts, results, state, as_of)
    contacts = contact_properties(facts)
    workspaces = {w: workspace_properties(f, results[w]) for w, f in sorted(facts.items())}

    report = SyncReport(
        dry_run=dry_run,
        companies=_sync_object(
            client, state, "companies", "companies", "company_key", companies, dry_run
        ),
        contacts=_sync_object(client, state, "contacts", "contacts", "email", contacts, dry_run),
        workspaces=_sync_object(
            client, state, "workspaces", ws_type, "workspace_id", workspaces, dry_run
        ),
        pql_workspaces=sum(r.is_pql for r in results.values()),
        pql_companies=sum(p.get("is_pql") == "true" for p in companies.values()),
    )

    # (from object, to object, association type) -> [(pair key, from key, to key)]
    wanted: dict[tuple[str, str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for workspace_id, f in sorted(facts.items()):
        if f.company_key:
            pair = f"ws:{workspace_id}|company:{f.company_key}"
            wanted[("workspaces", "companies", "default")].append(
                (pair, workspace_id, f.company_key)
            )
        for email in sorted(f.members):
            label = ADMIN_LABEL if email == f.admin_email else MEMBER_LABEL
            pair = f"ws:{workspace_id}|contact:{email}|{label}"
            wanted[("workspaces", "contacts", label)].append((pair, workspace_id, email))

    new = {k: [p for p in pairs if p[0] not in state.associations] for k, pairs in wanted.items()}
    report.associations_to_send = sum(len(pairs) for pairs in new.values())
    if dry_run or not report.associations_to_send:
        return report

    for (from_name, to_name, kind), pairs in new.items():
        if not pairs:
            continue
        to_type = "companies" if to_name == "companies" else "contacts"
        if kind == "default":
            type_id, category = (
                default_association_type(client, ws_type, to_type),
                "HUBSPOT_DEFINED",
            )
        else:
            type_id, category = _label_type(client, ws_type, to_type, kind), "USER_DEFINED"
        ready: list[tuple[str, AssociationInput]] = []
        for pair_key, from_key, to_key in pairs:
            from_id = state.record_id(from_name, from_key)
            to_id = state.record_id(to_name, to_key)
            if from_id is None or to_id is None:
                report.associations_skipped_missing_record += 1
                continue
            ready.append((pair_key, AssociationInput(from_id, to_id)))
        for chunk in batched(ready, MAX_ASSOCIATION_BATCH):
            report.associations_created += client.batch_associate(
                ws_type, to_type, type_id, category, [p for _, p in chunk]
            )
            state.associations.update(k for k, _ in chunk)
    return report
