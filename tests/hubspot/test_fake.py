from __future__ import annotations

import pytest

from pipelineos.hubspot.client import JSON, AssociationInput, HubSpotError, UpsertInput
from pipelineos.hubspot.fake import FakeHubSpot


def unique_prop(name: str, group: str = "companyinformation") -> JSON:
    return {
        "name": name,
        "label": name,
        "groupName": group,
        "type": "string",
        "fieldType": "text",
        "hasUniqueValue": True,
    }


def workspace_schema() -> JSON:
    return {
        "name": "workspace",
        "labels": {"singular": "Workspace", "plural": "Workspaces"},
        "primaryDisplayProperty": "workspace_id",
        "requiredProperties": ["workspace_id"],
        "searchableProperties": ["workspace_id"],
        "properties": [
            {"name": "workspace_id", "label": "Workspace ID", "hasUniqueValue": True},
            {"name": "seats", "label": "Seats", "type": "number", "fieldType": "number"},
        ],
        "associatedObjects": ["0-2", "0-1"],
    }


def test_companies_cannot_upsert_by_domain() -> None:
    hs = FakeHubSpot()
    with pytest.raises(HubSpotError, match="not a unique-value property"):
        hs.batch_upsert("companies", "domain", [UpsertInput("acme.io", {"name": "Acme"})])


def test_upsert_creates_then_updates_by_unique_key() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    first = hs.batch_upsert("companies", "company_key", [UpsertInput("acme.io", {"name": "Acme"})])
    second = hs.batch_upsert(
        "companies", "company_key", [UpsertInput("acme.io", {"name": "Acme Inc"})]
    )
    assert first.created == 1 and second.updated == 1
    assert first.results[0].record_id == second.results[0].record_id
    record = hs.records["companies"][first.results[0].record_id]
    assert record == {"name": "Acme Inc", "company_key": "acme.io"}


def test_contacts_upsert_by_email_is_case_insensitive() -> None:
    hs = FakeHubSpot()
    a = hs.batch_upsert("contacts", "email", [UpsertInput("Ana@Acme.io", {"firstname": "Ana"})])
    b = hs.batch_upsert("contacts", "email", [UpsertInput("ana@acme.io", {"lastname": "Diaz"})])
    assert a.results[0].record_id == b.results[0].record_id
    assert b.updated == 1


def test_per_row_errors_do_not_fail_the_batch() -> None:
    hs = FakeHubSpot()
    result = hs.batch_upsert(
        "contacts",
        "email",
        [
            UpsertInput("ok@x.io", {"lifecyclestage": "lead"}),
            UpsertInput("bad@x.io", {"lifecyclestage": "pql"}),
            UpsertInput("nope@x.io", {"favourite_colour": "red"}),
        ],
    )
    assert result.created == 1
    assert [e.key for e in result.errors] == ["bad@x.io", "nope@x.io"]
    assert "not a valid option" in result.errors[0].message
    assert "does not exist" in result.errors[1].message


def test_value_type_checks() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    hs.create_property(
        "companies",
        {
            "name": "is_pql",
            "label": "PQL",
            "groupName": "companyinformation",
            "type": "bool",
            "fieldType": "booleancheckbox",
        },
    )
    result = hs.batch_upsert(
        "companies",
        "company_key",
        [
            UpsertInput("a.io", {"numberofemployees": "many"}),
            UpsertInput("b.io", {"is_pql": "yes"}),
            UpsertInput("c.io", {"numberofemployees": "", "is_pql": "true"}),
            UpsertInput("d.io", {"lifecyclestage": "lead;customer"}),
        ],
    )
    assert [e.key for e in result.errors] == ["a.io", "b.io"]
    assert result.created == 2


def test_unique_value_conflict_across_records() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    hs.create_property("companies", unique_prop("stripe_id"))
    hs.batch_upsert("companies", "company_key", [UpsertInput("a.io", {"stripe_id": "cus_1"})])
    result = hs.batch_upsert(
        "companies", "company_key", [UpsertInput("b.io", {"stripe_id": "cus_1"})]
    )
    assert result.errors and "already belongs" in result.errors[0].message


def test_batch_rules_mirror_hubspot() -> None:
    hs = FakeHubSpot()
    with pytest.raises(HubSpotError, match="duplicate"):
        hs.batch_upsert("contacts", "email", [UpsertInput("a@x.io", {})] * 2)
    with pytest.raises(HubSpotError, match="cap"):
        hs.batch_upsert("contacts", "email", [UpsertInput(f"{i}@x.io", {}) for i in range(101)])


def test_property_rules() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    with pytest.raises(HubSpotError, match="already exists"):
        hs.create_property("companies", unique_prop("company_key"))
    with pytest.raises(HubSpotError, match="missing"):
        hs.create_property("companies", {"name": "x"})
    with pytest.raises(HubSpotError, match="group"):
        hs.create_property("companies", unique_prop("y", group="nope"))
    with pytest.raises(HubSpotError, match="fieldType"):
        hs.create_property("companies", {**unique_prop("z"), "fieldType": "number"})
    with pytest.raises(HubSpotError, match="hasUniqueValue"):
        hs.update_property(
            "companies",
            "company_key",
            {"type": "string", "fieldType": "text", "hasUniqueValue": False},
        )
    with pytest.raises(HubSpotError, match="required"):
        hs.update_property("companies", "company_key", {"label": "Key"})
    with pytest.raises(HubSpotError, match="not found"):
        hs.update_property("companies", "ghost", {"type": "string", "fieldType": "text"})
    updated = hs.update_property(
        "companies", "company_key", {"type": "string", "fieldType": "text", "label": "Key"}
    )
    assert updated["label"] == "Key"


def test_unique_property_limit_is_ten() -> None:
    hs = FakeHubSpot()
    for i in range(10):
        hs.create_property("companies", unique_prop(f"key_{i}"))
    with pytest.raises(HubSpotError, match="10 unique"):
        hs.create_property("companies", unique_prop("key_10"))


def test_groups() -> None:
    hs = FakeHubSpot()
    hs.create_property_group("contacts", {"name": "pipelineos", "label": "PipelineOS"})
    with pytest.raises(HubSpotError):
        hs.create_property_group("contacts", {"name": "pipelineos", "label": "again"})
    hs.update_property_group("contacts", "pipelineos", {"label": "Renamed", "name": "ignored"})
    groups = {g["name"]: g for g in hs.list_property_groups("contacts")}
    assert groups["pipelineos"]["label"] == "Renamed"
    with pytest.raises(HubSpotError):
        hs.update_property_group("contacts", "ghost", {})


def test_custom_object_schema_lifecycle() -> None:
    hs = FakeHubSpot(portal=99)
    created = hs.create_schema(workspace_schema())
    type_id = created["objectTypeId"]
    assert created["fullyQualifiedName"] == "p99_workspace"
    assert {p["name"] for p in created["properties"]} == {"workspace_id", "seats"}
    # resolvable by id and by fully-qualified name
    assert hs.list_properties("p99_workspace") == hs.list_properties(type_id)
    with pytest.raises(HubSpotError, match="cannot be changed"):
        hs.update_schema(type_id, {"name": "workspaces"})
    with pytest.raises(HubSpotError, match="unknown"):
        hs.update_schema(type_id, {"searchableProperties": ["ghost"]})
    hs.update_schema(type_id, {"searchableProperties": ["workspace_id", "seats"]})
    assert hs.list_schemas()[0]["searchableProperties"] == ["workspace_id", "seats"]
    with pytest.raises(HubSpotError, match="already exists"):
        hs.create_schema(workspace_schema())
    with pytest.raises(HubSpotError, match="not found"):
        hs.update_schema("0-1", {})
    # default association types exist both ways
    assert hs.list_association_labels(type_id, "companies")
    assert hs.list_association_labels("companies", type_id)


def test_schema_validation() -> None:
    hs = FakeHubSpot()
    with pytest.raises(HubSpotError, match="missing"):
        hs.create_schema({"name": "x"})
    bad = workspace_schema()
    bad["requiredProperties"] = ["ghost"]
    with pytest.raises(HubSpotError, match="unknown"):
        hs.create_schema(bad)
    with pytest.raises(HubSpotError, match="unknown object type"):
        hs.list_properties("2-404")


def test_pipelines_and_stages() -> None:
    hs = FakeHubSpot()
    assert [p["id"] for p in hs.list_pipelines("deals")] == ["default"]
    created = hs.create_pipeline(
        "deals",
        {
            "label": "New Business",
            "displayOrder": 1,
            "stages": [
                {
                    "stageId": "nb_discovery",
                    "label": "Discovery",
                    "displayOrder": 0,
                    "metadata": {"probability": 0.1},
                }
            ],
        },
    )
    pid = created["id"]
    assert created["stages"][0] == {
        "id": "nb_discovery",
        "label": "Discovery",
        "displayOrder": 0,
        "metadata": {"probability": "0.1"},
    }
    hs.create_stage(
        "deals",
        pid,
        {
            "stageId": "nb_won",
            "label": "Won",
            "displayOrder": 1,
            "metadata": {"probability": 1.0, "isClosed": True},
        },
    )
    hs.update_stage(
        "deals", pid, "nb_discovery", {"label": "Discovery call", "metadata": {"probability": 0.15}}
    )
    hs.update_pipeline("deals", pid, {"label": "New Business v2"})
    stored = next(p for p in hs.list_pipelines("deals") if p["id"] == pid)
    assert stored["label"] == "New Business v2"
    assert stored["stages"][0]["metadata"]["probability"] == "0.15"
    with pytest.raises(HubSpotError, match="already exists"):
        hs.create_pipeline("deals", {"label": "New Business v2", "displayOrder": 2})
    with pytest.raises(HubSpotError, match="already used"):
        hs.create_stage(
            "deals",
            pid,
            {"stageId": "nb_won", "label": "x", "displayOrder": 2, "metadata": {"probability": 1}},
        )
    with pytest.raises(HubSpotError, match="probability"):
        hs.create_stage("deals", pid, {"label": "x", "displayOrder": 2, "metadata": {}})
    with pytest.raises(HubSpotError, match="outside"):
        hs.update_stage("deals", pid, "nb_won", {"metadata": {"probability": 2}})
    with pytest.raises(HubSpotError, match="stage"):
        hs.update_stage("deals", pid, "ghost", {})
    with pytest.raises(HubSpotError, match="pipeline"):
        hs.update_pipeline("deals", "ghost", {})


def test_association_labels_and_records() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    contact = hs.batch_upsert("contacts", "email", [UpsertInput("a@acme.io", {})]).results[0]
    company = hs.batch_upsert("companies", "company_key", [UpsertInput("acme.io", {})]).results[0]
    made = hs.create_association_label(
        "contacts", "companies", {"label": "Champion", "name": "champion"}
    )["results"]
    type_id = made[0]["typeId"]
    with pytest.raises(HubSpotError, match="already exists"):
        hs.create_association_label("contacts", "companies", {"label": "C", "name": "champion"})
    with pytest.raises(HubSpotError, match="invalid label"):
        hs.create_association_label("contacts", "companies", {"label": "B", "name": "bad-name"})
    pair = AssociationInput(contact.record_id, company.record_id)
    assert hs.batch_associate("contacts", "companies", type_id, "USER_DEFINED", [pair]) == 1
    assert ("contacts", contact.record_id, "companies", company.record_id, type_id) in (
        hs.associations
    )
    with pytest.raises(HubSpotError, match="unknown association type"):
        hs.batch_associate("contacts", "companies", 999_999, "USER_DEFINED", [pair])
    with pytest.raises(HubSpotError, match="record missing"):
        hs.batch_associate(
            "contacts",
            "companies",
            type_id,
            "USER_DEFINED",
            [AssociationInput("404", company.record_id)],
        )
    with pytest.raises(HubSpotError, match="too large"):
        hs.batch_associate("contacts", "companies", type_id, "USER_DEFINED", [pair] * 2001)


def test_label_limit_per_pair() -> None:
    hs = FakeHubSpot()
    for i in range(10):
        hs.create_association_label("contacts", "companies", {"label": f"L{i}", "name": f"l{i}"})
    with pytest.raises(HubSpotError, match="limit"):
        hs.create_association_label("contacts", "companies", {"label": "L10", "name": "l10"})


def test_scripted_failures_and_call_log() -> None:
    hs = FakeHubSpot()
    hs.fail_next.append(HubSpotError(503, "UNAVAILABLE", "down"))
    with pytest.raises(HubSpotError, match="down"):
        hs.list_schemas()
    hs.portal_id()
    hs.create_property_group("deals", {"name": "g", "label": "G"})
    assert hs.calls == [("list_schemas", ""), ("get_portal", ""), ("create_group", "deals")]
    assert hs.writes == [("create_group", "deals")]


def test_validation_rules_are_enforced_on_writes() -> None:
    hs = FakeHubSpot()
    hs.create_property("companies", unique_prop("company_key"))
    hs.create_property(
        "companies",
        {
            "name": "pql_score",
            "label": "PQL",
            "groupName": "companyinformation",
            "type": "number",
            "fieldType": "number",
        },
    )
    hs.put_validation_rule("0-2", "company_key", "DOMAIN", {"ruleArguments": []})
    hs.put_validation_rule("0-2", "company_key", "FORMAT", {"ruleArguments": ["LOWER"]})
    hs.put_validation_rule("companies", "pql_score", "MAX_NUMBER", {"ruleArguments": [100]})
    hs.put_validation_rule("companies", "pql_score", "MIN_NUMBER", {"ruleArguments": ["0"]})
    hs.put_validation_rule("companies", "name", "MIN_LENGTH", {"ruleArguments": ["2"]})
    hs.put_validation_rule("companies", "name", "MAX_LENGTH", {"ruleArguments": ["40"]})
    hs.put_validation_rule("companies", "name", "WHITESPACE", {"ruleArguments": ["TRIM"]})
    hs.put_validation_rule("companies", "domain", "REGEX", {"ruleArguments": ["\\.", "dot"]})
    hs.put_validation_rule("companies", "industry", "SPECIAL_CHARACTERS", {})
    hs.create_property("contacts", unique_prop("backup_email", "contactinformation"))
    hs.put_validation_rule("0-1", "backup_email", "EMAIL", {"ruleArguments": []})
    hs.put_validation_rule("0-1", "firstname", "FORMAT", {"ruleArguments": ["UPPER"]})
    with pytest.raises(HubSpotError, match="not found"):
        hs.put_validation_rule("0-2", "ghost", "DOMAIN", {})

    rows = hs.list_validation_rules("0-2")
    assert {
        "propertyName": "pql_score",
        "ruleType": "MAX_NUMBER",
        "ruleArguments": ["100"],
        "shouldApplyNormalization": False,
    } in rows

    result = hs.batch_upsert(
        "companies",
        "company_key",
        [
            UpsertInput("acme.io", {"pql_score": "55", "name": "Acme", "domain": "acme.io"}),
            UpsertInput("Acme.IO", {}),
            UpsertInput("not a domain", {}),
            UpsertInput("b.io", {"pql_score": "101"}),
            UpsertInput("c.io", {"pql_score": "-1"}),
            UpsertInput("d.io", {"name": "x"}),
            UpsertInput("e.io", {"name": " padded"}),
            UpsertInput("f.io", {"name": "y" * 41}),
            UpsertInput("g.io", {"domain": "nodot"}),
        ],
    )
    assert result.created == 1
    assert [e.key for e in result.errors] == [
        "Acme.IO",
        "not a domain",
        "b.io",
        "c.io",
        "d.io",
        "e.io",
        "f.io",
        "g.io",
    ]
    contacts = hs.batch_upsert(
        "contacts",
        "email",
        [
            UpsertInput("a@x.io", {"backup_email": "nope", "firstname": "ANA"}),
            UpsertInput("b@x.io", {"firstname": "Ana"}),
        ],
    )
    assert [e.key for e in contacts.errors] == ["a@x.io", "b@x.io"]
