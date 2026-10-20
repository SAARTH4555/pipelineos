from __future__ import annotations

from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import Action, Op, build_plan
from pipelineos.spec import PortalSpec
from tests.portal.conftest import converged


def find(actions: list[Action], kind: str, name: str, op: Op | None = None) -> Action:
    matches = [a for a in actions if a.kind == kind and a.name == name]
    if op is not None:
        matches = [a for a in matches if a.op is op]
    assert matches, f"no {kind} {name} {op}"
    return matches[0]


def test_fresh_portal_plans_every_create(spec: PortalSpec) -> None:
    hs = FakeHubSpot()
    plan = build_plan(spec, read_live(hs, spec))
    counts = plan.counts()
    assert (
        counts[Op.CREATE] == 3 + 24 + 1 + 8 + 2 + 4
    )  # groups, props, schema, rules, pipelines, labels
    assert counts[Op.UPDATE] == counts[Op.MANUAL] == 0
    # the default "Sales Pipeline" is left alone, not deleted
    assert find(plan.actions, "pipeline", "Sales Pipeline").op is Op.UNMANAGED
    assert hs.writes == []  # planning only reads


def test_plan_is_in_dependency_order(spec: PortalSpec) -> None:
    actions = build_plan(spec, read_live(FakeHubSpot(), spec)).changes
    index = {(a.kind, a.object_ref, a.name, a.parent): i for i, a in enumerate(actions)}
    assert (
        index[("group", "contacts", "pipelineos_lead", "")]
        < index[("property", "contacts", "product_user_id", "")]
    )
    assert (
        index[("schema", "workspace", "workspace", "")]
        < index[("validation", "workspace", "REGEX", "workspace_id")]
    )
    assert (
        index[("property", "companies", "company_key", "")]
        < index[("validation", "companies", "DOMAIN", "company_key")]
    )
    kinds = [a.kind for a in actions]
    assert kinds.index("label") > max(i for i, k in enumerate(kinds) if k == "pipeline")


def test_converged_portal_plans_no_changes(spec: PortalSpec) -> None:
    plan = build_plan(spec, converged(spec))
    assert plan.changes == []
    assert plan.manual == []
    assert plan.counts()[Op.UNMANAGED] == 0


def test_property_drift_kinds(spec: PortalSpec) -> None:
    live = converged(spec)
    contacts = live.objects["contacts"].properties
    contacts["persona"]["label"] = "Buyer persona"
    contacts["routing_reason"]["type"] = "number"
    contacts["product_user_id"]["hasUniqueValue"] = False
    contacts["segment"]["hubspotDefined"] = True
    contacts["signup_source"]["options"] = contacts["signup_source"]["options"] + [
        {"label": "Podcast", "value": "podcast", "displayOrder": 9}
    ]
    contacts["legacy_score"] = {"name": "legacy_score", "type": "number"}
    actions = build_plan(spec, live).actions

    persona = find(actions, "property", "persona")
    assert persona.op is Op.UPDATE
    assert "label: 'Buyer persona' -> 'Persona'" in persona.detail
    assert persona.payload["type"] == "enumeration"  # HubSpot wants type+fieldType on update

    assert "delete and recreate" in find(actions, "property", "routing_reason", Op.MANUAL).detail
    assert "never change" in find(actions, "property", "product_user_id", Op.MANUAL).detail
    assert "HubSpot-defined" in find(actions, "property", "segment", Op.MANUAL).detail
    signup = find(actions, "property", "signup_source", Op.UPDATE)
    assert "removes ['podcast']" in signup.detail
    assert find(actions, "property", "legacy_score").op is Op.UNMANAGED


def test_group_and_validation_drift(spec: PortalSpec) -> None:
    live = converged(spec)
    live.objects["contacts"].groups["pipelineos_lead"]["label"] = "Leads"
    live.objects["contacts"].groups["old_stuff"] = {"name": "old_stuff", "label": "Old"}
    rules = live.objects["companies"].rules
    rules["pql_score"]["MAX_NUMBER"]["ruleArguments"] = ["1000"]
    rules["account_segment"] = {"MIN_LENGTH": {"ruleArguments": ["2"]}}
    del live.objects["deals"].rules["arr"]
    actions = build_plan(spec, live).actions

    assert find(actions, "group", "pipelineos_lead").op is Op.UPDATE
    assert find(actions, "group", "old_stuff").op is Op.UNMANAGED
    max_rule = find(actions, "validation", "MAX_NUMBER", Op.UPDATE)
    assert max_rule.parent == "pql_score" and "['1000'] -> ['100']" in max_rule.detail
    assert find(actions, "validation", "MIN_LENGTH").op is Op.UNMANAGED
    arr_rule = next(a for a in actions if a.kind == "validation" and a.parent == "arr")
    assert arr_rule.op is Op.CREATE


def test_existing_schema_drift(spec: PortalSpec) -> None:
    live = converged(spec)
    schema = live.schemas["workspace"]
    schema["labels"] = {"singular": "Team", "plural": "Teams"}
    schema["associatedObjects"] = ["0-1"]
    schema["searchableProperties"] = ["workspace_id"]
    del live.objects["workspace"].properties["last_active_date"]
    live.schemas["legacy_object"] = {"name": "legacy_object"}
    actions = build_plan(spec, live).actions

    manual = [a.detail for a in actions if a.kind == "schema" and a.op is Op.MANUAL]
    assert any("labels cannot change" in d for d in manual)
    assert any("associated objects" in d for d in manual)
    update = find(actions, "schema", "workspace", Op.UPDATE)
    assert update.payload == {"searchableProperties": ["workspace_id", "workspace_name"]}
    new_prop = find(actions, "property", "last_active_date", Op.CREATE)
    assert new_prop.payload["groupName"] == "workspace_information"
    assert actions.index(new_prop) < actions.index(update)
    assert find(actions, "schema", "legacy_object").op is Op.UNMANAGED


def test_schema_associations_read_from_association_rows(spec: PortalSpec) -> None:
    live = converged(spec)
    schema = live.schemas["workspace"]
    del schema["associatedObjects"]
    schema["associations"] = [
        {"fromObjectTypeId": "2-900", "toObjectTypeId": "0-1"},
        {"fromObjectTypeId": "2-900", "toObjectTypeId": "0-2"},
        {"fromObjectTypeId": "0-2", "toObjectTypeId": "2-900"},
    ]
    assert build_plan(spec, live).manual == []


def test_pipeline_and_stage_drift(spec: PortalSpec) -> None:
    live = converged(spec)
    new_business, expansion = live.pipelines["deals"]
    new_business["displayOrder"] = 5
    new_business["stages"][1]["metadata"]["probability"] = "0.25"
    new_business["stages"][2]["label"] = "Security"
    new_business["stages"][3]["metadata"]["isClosed"] = "true"
    new_business["stages"].append(
        {
            "id": "nb_legacy",
            "label": "Legacy",
            "displayOrder": 9,
            "metadata": {"probability": "0.5"},
        }
    )
    removed = expansion["stages"].pop(1)  # ex_scoping
    actions = build_plan(spec, live).actions

    pipeline = find(actions, "pipeline", "New Business", Op.UPDATE)
    assert pipeline.payload == {"displayOrder": 1} and pipeline.live_id == "p0"
    tech = find(actions, "stage", "nb_technical_eval", Op.UPDATE)
    assert "probability: '0.25' -> '0.3'" in tech.detail
    assert "stageId" not in tech.payload
    assert "label: 'Security'" in find(actions, "stage", "nb_security_review").detail
    assert "isClosed" in find(actions, "stage", "nb_proposal").detail
    assert find(actions, "stage", "nb_legacy").op is Op.UNMANAGED
    created = find(actions, "stage", removed["id"], Op.CREATE)
    assert created.payload["stageId"] == "ex_scoping" and created.live_id == "p1"


def test_stage_id_clashes_are_manual(spec: PortalSpec) -> None:
    live = converged(spec)
    new_business, expansion = live.pipelines["deals"]
    moved = expansion["stages"].pop(0)  # ex_identified now lives in New Business
    new_business["stages"].append(moved)
    actions = build_plan(spec, live).actions
    clash = find(actions, "stage", "ex_identified", Op.MANUAL)
    assert "already used by pipeline 'New Business'" in clash.detail

    live = converged(spec)
    live.pipelines["deals"].pop(1)  # Expansion missing...
    live.pipelines["deals"][0]["stages"].append(
        {"id": "ex_identified", "label": "x", "displayOrder": 9, "metadata": {"probability": "0"}}
    )  # ...and one of its stage ids is taken
    pipeline = find(build_plan(spec, live).actions, "pipeline", "Expansion", Op.MANUAL)
    assert "already used" in pipeline.detail


def test_labels(spec: PortalSpec) -> None:
    live = converged(spec)
    live.labels[("contacts", "companies")].append(
        {"category": "USER_DEFINED", "typeId": 7, "label": "Billing contact"}
    )
    live.labels[("contacts", "companies")].append(
        {"category": "HUBSPOT_DEFINED", "typeId": 1, "label": "Primary"}
    )
    live.labels[("workspace", "contacts")] = []
    actions = build_plan(spec, live).actions
    assert find(actions, "label", "Billing contact").op is Op.UNMANAGED
    assert not [a for a in actions if a.name == "Primary"]
    admin = find(actions, "label", "Admin", Op.CREATE)
    assert admin.payload == {
        "label": "Admin",
        "name": "workspace_admin",
        "inverseLabel": "Admin of",
    }
    assert admin.target == "workspace -> contacts: Admin"


def test_action_targets_read_well(spec: PortalSpec) -> None:
    actions = build_plan(spec, read_live(FakeHubSpot(), spec)).actions
    targets = {a.kind: a.target for a in actions}
    assert targets["schema"] == "workspace"
    assert targets["pipeline"].startswith("deals: ")
    assert find(actions, "validation", "DOMAIN").target == "companies.company_key DOMAIN"
    assert find(actions, "property", "arr").target == "deals.arr"
    stage = Action(Op.CREATE, "stage", "deals", "nb_x", parent="New Business")
    assert stage.target == "deals / New Business / nb_x"
