"""SignalScout -> PipelineOS: the hand-off files load, signal columns and all.

``tests/fixtures/signalscout`` is real output of ``signalscout run --offline`` (PipelineOS's
golden company columns, then ``signal_*`` columns; contacts.csv header-only). If either side
changes its format, this is the test that breaks.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pipelineos.cli import app
from pipelineos.data.load import company_inputs, load
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import build_plan
from pipelineos.spec import PortalSpec, load_spec

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "tests" / "fixtures" / "signalscout"
GTM_SPEC = ROOT / "portal" / "gtm-consultancy.yaml"


def configured(spec: PortalSpec) -> FakeHubSpot:
    hs = FakeHubSpot()
    live = read_live(hs, spec)
    assert apply_plan(hs, build_plan(spec, live), live).ok
    return hs


def declared(spec: PortalSpec) -> frozenset[str]:
    return frozenset(p.name for p in spec.objects["companies"].properties)


def test_signal_columns_load_into_the_consultancy_portal() -> None:
    spec = load_spec(GTM_SPEC)
    hs = configured(spec)
    report = load(hs, HANDOFF, declared(spec))
    assert report.companies.created == 3 and report.companies.errors == []
    assert report.contacts.created == 0  # SignalScout never sends people
    record = hs.records["companies"][report.companies.ids["ledgerline.io"]]
    assert record["signal_tier"] == "A" and record["signal_score"] == "79"
    assert record["signal_crm"] == "salesforce" and record["account_segment"] == "mid_market"
    assert record["signal_reasons"].startswith("120 people, inside the 50-500 band")
    assert record["signal_funding_amount"] == "30500000"
    assert record["signal_checked_at"] == "2026-10-15"


def test_undeclared_columns_are_never_sent() -> None:
    quarry = load_spec(ROOT / "portal" / "pipelineos.yaml")
    hs = configured(quarry)  # Quarry's portal has no signal_* properties
    report = load(hs, HANDOFF, declared(quarry))
    assert report.companies.created == 3 and report.companies.errors == []
    record = hs.records["companies"][report.companies.ids["ledgerline.io"]]
    assert not any(k.startswith("signal_") for k in record)


def test_passthrough_skips_blanks_and_base_fields() -> None:
    rows = [
        {
            "company_key": "a.io",
            "name": "A",
            "signal_tier": "B",
            "signal_crm": "",
            "numberofemployees": "70",
            "unknown": "x",
        }
    ]
    (only,) = company_inputs(rows, frozenset({"signal_tier", "signal_crm", "name"}))
    assert only.properties == {
        "name": "A",
        "numberofemployees": "70",
        "account_segment": "mid_market",
        "signal_tier": "B",
    }


def test_cli_load_with_the_consultancy_spec() -> None:
    result = CliRunner().invoke(
        app, ["load", str(HANDOFF), "--spec", str(GTM_SPEC), "--fake", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "companies: 3 created, 0 updated, 0 failed" in result.stdout
