from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from pipelineos.hubspot.client import HubSpotError
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import build_plan
from pipelineos.spec import PortalSpec
from pipelineos.usage.events import Event
from pipelineos.usage.score import PqlResult, WorkspaceFacts, aggregate, load_pql_config, score
from pipelineos.usage.state import SyncState, content_hash, load_state, save_state
from pipelineos.usage.sync import sync

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_pql_config(ROOT / "portal" / "pql.yaml")
AS_OF = date(2026, 10, 1)


def configured(spec: PortalSpec) -> FakeHubSpot:
    hs = FakeHubSpot()
    live = read_live(hs, spec)
    assert apply_plan(hs, build_plan(spec, live), live).ok
    return hs


def ev(workspace: str, type_: str, days_ago: int, email: str, value: str = "") -> Event:
    user = "usr_" + "".join(c for c in email if c.isalnum())[:10].lower().ljust(8, "x")
    return Event(workspace, user, email, type_, AS_OF - timedelta(days=days_ago), value)  # type: ignore[arg-type]


def team_events() -> list[Event]:
    """ws_team0001: a 4-person evaluating team at acme.io; ws_solo0001: one gmail hobbyist."""
    events = [ev("ws_team0001", "signup", 60, "ana@acme.io", "Acme CI")]
    for email in ("bo@acme.io", "cy@acme.io", "di@acme.io"):
        events.append(ev("ws_team0001", "seat_added", 55, email))
    for email in ("ana@acme.io", "bo@acme.io", "cy@acme.io"):
        events.append(ev("ws_team0001", "session", 1, email))
    events += [ev("ws_team0001", "ci_pipeline_connected", 5, "bo@acme.io")]
    events += [ev("ws_team0001", "flaky_test_quarantined", 3, "cy@acme.io")] * 45
    events += [
        ev("ws_solo0001", "signup", 30, "solo@gmail.com", "Solo's workspace"),
        ev("ws_solo0001", "session", 2, "solo@gmail.com"),
    ]
    return events


def scored(
    events: list[Event], as_of: date = AS_OF
) -> tuple[dict[str, WorkspaceFacts], dict[str, PqlResult]]:
    facts = aggregate(events, as_of)
    return facts, {w: score(f, CONFIG, as_of, 120) for w, f in facts.items()}


def ws_type(hs: FakeHubSpot) -> str:
    return str(next(s["objectTypeId"] for s in hs.list_schemas() if s["name"] == "workspace"))


def test_first_sync_writes_everything_with_rollups_and_labels(spec: PortalSpec) -> None:
    hs = configured(spec)
    facts, results = scored(team_events())
    state = SyncState()
    report = sync(hs, facts, results, state, AS_OF, dry_run=False)

    assert (report.workspaces.created, report.companies.created, report.contacts.created) == (
        2,
        1,
        5,
    )
    assert report.pql_workspaces == 1 and report.pql_companies == 1
    t = ws_type(hs)
    team_id = state.record_id("workspaces", "ws_team0001")
    assert team_id is not None
    team = hs.records[t][team_id]
    assert team["is_pql"] == "true" and team["seats"] == "4" and team["plan"] == "free"
    assert "Connected at least one CI pipeline" in team["pql_reasons"]

    company_id = state.record_id("companies", "acme.io")
    assert company_id is not None
    company = hs.records["companies"][company_id]
    assert company["is_pql"] == "true" and company["pql_since"] == "2026-10-01"
    assert company["workspace_count"] == "1" and company["product_plan"] == "free"

    ana_id = state.record_id("contacts", "ana@acme.io")
    assert ana_id is not None
    assert hs.records["contacts"][ana_id]["product_user_id"].startswith("usr_")
    assert "hs_lead_status" not in hs.records["contacts"][ana_id]  # left to the workflow

    labels = {x["label"]: x["typeId"] for x in hs.list_association_labels(t, "contacts")}
    assert (t, team_id, "contacts", ana_id, labels["Admin"]) in hs.associations
    bo_id = state.record_id("contacts", "bo@acme.io")
    assert (t, team_id, "contacts", bo_id, labels["Member"]) in hs.associations
    assert report.associations_created == 4 + 1 + 1  # members, company, solo admin


def test_second_sync_sends_nothing(spec: PortalSpec) -> None:
    hs = configured(spec)
    facts, results = scored(team_events())
    state = SyncState()
    sync(hs, facts, results, state, AS_OF, dry_run=False)
    writes = len(hs.writes)
    again = sync(hs, facts, results, state, AS_OF, dry_run=False)
    assert again.writes == 0 and len(hs.writes) == writes
    assert again.workspaces.skipped_unchanged == 2


def test_only_what_changed_is_sent(spec: PortalSpec) -> None:
    hs = configured(spec)
    events = team_events()
    state = SyncState()
    sync(hs, *scored(events), state, AS_OF, dry_run=False)

    events.append(ev("ws_solo0001", "seat_added", 1, "pal@gmail.com"))
    report = sync(hs, *scored(events), state, AS_OF, dry_run=False)
    assert report.workspaces.to_send == 1  # the solo workspace now has 2 seats
    assert report.contacts.to_send == 1  # the new member
    assert report.companies.to_send == 0  # acme.io roll-up did not change
    assert report.associations_to_send == 1


def test_dry_run_writes_nothing_and_keeps_state_empty(spec: PortalSpec) -> None:
    hs = configured(spec)
    writes = len(hs.writes)
    state = SyncState()
    report = sync(hs, *scored(team_events()), state, AS_OF, dry_run=True)
    assert report.workspaces.to_send == 2 and report.associations_to_send == 6
    assert len(hs.writes) == writes and state.records == {} and state.associations == set()


def test_pql_since_keeps_the_first_date(spec: PortalSpec) -> None:
    hs = configured(spec)
    events = team_events()
    state = SyncState()
    sync(hs, *scored(events), state, AS_OF, dry_run=False)
    later = AS_OF + timedelta(days=1)
    events.append(ev("ws_team0001", "session", 0, "di@acme.io"))
    sync(hs, *scored(events, later), state, later, dry_run=False)
    assert state.pql_since == {"acme.io": "2026-10-01"}


def test_failed_rows_skip_their_associations_and_retry_later(spec: PortalSpec) -> None:
    hs = configured(spec)
    events = team_events()
    bad = Event("ws_team0001", "USR_BAD", "eve@acme.io", "seat_added", AS_OF - timedelta(days=3))
    facts, results = scored([*events, bad])
    state = SyncState()
    report = sync(hs, facts, results, state, AS_OF, dry_run=False)
    assert [e.key for e in report.contacts.errors] == ["eve@acme.io"]  # fails the REGEX rule
    assert report.associations_skipped_missing_record == 1
    assert not any("eve@acme.io" in k for k in state.associations)


def test_unconfigured_portal_is_refused(spec: PortalSpec) -> None:
    facts, results = scored(team_events())
    with pytest.raises(HubSpotError, match="run pipelineos apply"):
        sync(FakeHubSpot(), facts, results, SyncState(), AS_OF, dry_run=False)
    hs = configured(spec)
    t = ws_type(hs)
    hs._labels[(t, "contacts")] = [x for x in hs._labels[(t, "contacts")] if x["label"] != "Member"]
    with pytest.raises(HubSpotError, match="'Member' missing"):
        sync(hs, facts, results, SyncState(), AS_OF, dry_run=False)


def test_state_round_trip(tmp_path: Path) -> None:
    state = SyncState()
    state.remember("contacts", "a@x.io", {"p": "1"}, "101")
    state.associations.add("ws:1|contact:a@x.io|Admin")
    state.pql_since["x.io"] = "2026-10-01"
    path = tmp_path / "state.json"
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.records == state.records and loaded.associations == state.associations
    assert loaded.unchanged("contacts", "a@x.io", {"p": "1"})
    assert not loaded.unchanged("contacts", "a@x.io", {"p": "2"})
    assert load_state(tmp_path / "missing.json").records == {}
    assert content_hash({"b": "1", "a": "2"}) == content_hash({"a": "2", "b": "1"})
