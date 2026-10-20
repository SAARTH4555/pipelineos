from __future__ import annotations

from pipelineos.hubspot.client import HubSpotError
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import Plan, build_plan
from pipelineos.portal.render import render_apply, render_checklist, render_plan
from pipelineos.spec import PortalSpec


def plan_for(hs: FakeHubSpot, spec: PortalSpec) -> Plan:
    return build_plan(spec, read_live(hs, spec))


def converge(hs: FakeHubSpot, spec: PortalSpec) -> None:
    live = read_live(hs, spec)
    result = apply_plan(hs, build_plan(spec, live), live)
    assert result.ok, result.error


def test_apply_from_empty_converges_and_is_idempotent(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    live = read_live(hs, spec)
    first = build_plan(spec, live)
    result = apply_plan(hs, first, live)
    assert result.ok and len(result.applied) == len(first.changes) == 42

    again = plan_for(hs, spec)
    assert again.changes == [] and again.manual == []
    writes_before = len(hs.writes)
    assert apply_plan(hs, again, read_live(hs, spec)).applied == []
    assert len(hs.writes) == writes_before

    # what landed is what the spec says
    workspace = next(s for s in hs.list_schemas() if s["name"] == "workspace")
    assert workspace["requiredProperties"] == ["workspace_id", "workspace_name"]
    rules = hs.list_validation_rules(workspace["objectTypeId"])
    assert rules[0]["ruleType"] == "REGEX"
    labels = hs.list_association_labels(workspace["objectTypeId"], "contacts")
    assert {x["label"] for x in labels} >= {"Admin", "Member"}


def test_spec_edits_apply_as_updates(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    converge(hs, spec)

    raw = spec.model_dump(by_alias=True)
    raw["objects"]["contacts"]["groups"][0]["label"] = "Leads & routing"
    persona = next(p for p in raw["objects"]["contacts"]["properties"] if p["name"] == "persona")
    persona["label"] = "Buyer persona"
    persona["options"].append({"value": "procurement", "label": "Procurement"})
    raw["objects"]["companies"]["properties"][5]["validation"][1]["arguments"] = ["99"]
    raw["custom_objects"][0]["searchable"].append("plan")
    raw["custom_objects"][0]["properties"].append(
        {"name": "region", "label": "Region", "type": "string", "field_type": "text"}
    )
    new_business, expansion = raw["pipelines"]["deals"]
    new_business["display_order"] = 3
    new_business["stages"][1]["probability"] = 0.35
    expansion["stages"].insert(
        2, {"id": "ex_legal", "label": "Legal", "probability": 0.6, "closed": False}
    )
    edited = PortalSpec.model_validate(raw)

    plan = plan_for(hs, edited)
    kinds = sorted((a.kind, a.op.value) for a in plan.changes)
    assert kinds == sorted(
        [
            ("group", "update"),
            ("property", "update"),
            ("validation", "update"),
            ("schema", "update"),
            ("property", "create"),
            ("pipeline", "update"),
            ("stage", "update"),  # nb_technical_eval probability
            ("stage", "create"),  # ex_legal
            # every Expansion stage after the insert shifts displayOrder
            ("stage", "update"),
            ("stage", "update"),
            ("stage", "update"),
        ]
    )
    live = read_live(hs, edited)
    assert apply_plan(hs, plan, live).ok
    assert plan_for(hs, edited).changes == []


def test_apply_stops_at_first_failure_and_resumes(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    live = read_live(hs, spec)
    plan = build_plan(spec, live)
    calls_so_far = len(hs.calls)
    # fail on the 6th write
    original = hs._call
    count = {"n": 0}

    def flaky(op: str, target: str) -> None:
        if not op.startswith(("list", "get")):
            count["n"] += 1
            if count["n"] == 6:
                hs.calls.append((op, target))
                raise HubSpotError(500, "INTERNAL", "boom")
        original(op, target)

    hs._call = flaky  # type: ignore[method-assign]
    result = apply_plan(hs, plan, live)
    assert not result.ok and len(result.applied) == 5
    assert "boom" in render_apply(result, len(plan.changes))
    assert len(hs.calls) > calls_so_far

    hs._call = original  # type: ignore[method-assign]
    resumed = plan_for(hs, spec)
    assert len(resumed.changes) == len(plan.changes) - 5
    converge(hs, spec)
    assert plan_for(hs, spec).changes == []


def test_unknown_custom_object_fails_cleanly(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    converge(hs, spec)
    raw = spec.model_dump(by_alias=True)
    raw["association_labels"].append(
        {"from": "workspace", "to": "deals", "name": "renewal", "label": "Renewal"}
    )
    edited = PortalSpec.model_validate(raw)
    live = read_live(hs, edited)
    plan = build_plan(edited, live)
    live.objects.pop("workspace")  # simulate a stale snapshot missing the type id
    result = apply_plan(hs, plan, live)
    assert not result.ok and result.failed is not None and result.failed.kind == "label"


def test_render_plan_and_checklist(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    text = render_plan(plan_for(hs, spec))
    assert "  + schema     workspace" in text
    assert "? pipeline   deals: Sales Pipeline" in text and "(left alone)" in text
    assert text.endswith("1 unmanaged (left alone), 0 unchanged.")
    converge(hs, spec)
    verbose = render_plan(plan_for(hs, spec), verbose=True)
    assert "property   contacts.persona" in verbose
    checklist = render_checklist(spec)
    assert "New Business / Proposal: arr, amount" in checklist
    assert "[ ] Permissions:" in checklist
    assert checklist.count("[ ] Pipelines") == 1
