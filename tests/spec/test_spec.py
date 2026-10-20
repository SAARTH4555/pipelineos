from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipelineos.spec import SpecError, load_spec
from pipelineos.spec.docs import render_field_dictionary
from pipelineos.spec.models import Option, PortalSpec

SPEC_PATH = Path(__file__).resolve().parents[2] / "portal" / "pipelineos.yaml"
RAW: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())


def problems_for(raw: dict[str, Any], tmp_path: Path) -> list[str]:
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecError) as info:
        load_spec(path)
    return info.value.problems


def mutated() -> dict[str, Any]:
    return copy.deepcopy(RAW)


def contact_prop(raw: dict[str, Any], name: str) -> dict[str, Any]:
    props: list[dict[str, Any]] = raw["objects"]["contacts"]["properties"]
    return next(p for p in props if p["name"] == name)


def test_shipped_spec_is_valid() -> None:
    spec = load_spec(SPEC_PATH)
    assert spec.custom_objects[0].name == "workspace"
    assert {p.label for p in spec.pipelines["deals"]} == {"New Business", "Expansion"}
    # every object that is upserted has a unique key
    company_keys = [p.name for p in spec.objects["companies"].properties if p.unique]
    assert company_keys == ["company_key"]


def test_option_shorthand_expands_label() -> None:
    assert Option.model_validate("github_marketplace").label == "Github marketplace"


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="not found"):
        load_spec(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("objects: [unclosed")
    with pytest.raises(SpecError, match="not valid YAML"):
        load_spec(bad)


@pytest.mark.parametrize(
    ("name", "fragment"),
    [("hs_custom", "hs_ prefix"), ("ProductUser", "snake_case"), ("1abc", "snake_case")],
)
def test_property_naming_convention(tmp_path: Path, name: str, fragment: str) -> None:
    raw = mutated()
    contact_prop(raw, "persona")["name"] = name
    assert any(fragment in p for p in problems_for(raw, tmp_path))


def test_field_type_must_match_type(tmp_path: Path) -> None:
    raw = mutated()
    contact_prop(raw, "routing_reason")["field_type"] = "number"
    assert any("not valid for type 'string'" in p for p in problems_for(raw, tmp_path))


def test_enumeration_rules(tmp_path: Path) -> None:
    raw = mutated()
    del contact_prop(raw, "persona")["options"]
    assert any("need options" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    contact_prop(raw, "routing_reason")["options"] = ["a"]
    assert any("only enumeration" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    contact_prop(raw, "persona")["options"] = ["a", "a"]
    assert any("duplicate option" in p for p in problems_for(raw, tmp_path))


def test_unique_must_be_string_and_rules_unique(tmp_path: Path) -> None:
    raw = mutated()
    raw["objects"]["deals"]["properties"][2]["unique"] = True  # seats_committed: number
    assert any("unique properties must be strings" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    contact_prop(raw, "product_user_id")["validation"].append({"rule": "REGEX"})
    assert any("one rule per rule type" in p for p in problems_for(raw, tmp_path))


def test_cross_object_checks_report_every_problem(tmp_path: Path) -> None:
    raw = mutated()
    contact_prop(raw, "persona")["group"] = "ghost_group"
    raw["objects"]["contacts"]["properties"].append(copy.deepcopy(contact_prop(raw, "segment")))
    raw["pipelines"]["deals"][1]["stages"][0]["id"] = "nb_discovery"
    raw["pipelines"]["deals"][0]["stages"][0]["required_properties"] = ["budget"]
    raw["association_labels"][0]["from"] = "tickets"
    raw["association_labels"].append(copy.deepcopy(raw["association_labels"][1]))
    problems = problems_for(raw, tmp_path)
    expected = [
        "unknown group 'ghost_group'",
        "duplicate properties ['segment']",
        "stage id 'nb_discovery' is used twice",
        "requires undefined properties ['budget']",
        "references unknown 'tickets'",
        "'workspace_member' is defined twice",
    ]
    for fragment in expected:
        assert any(fragment in p for p in problems), fragment


def test_pipeline_rules(tmp_path: Path) -> None:
    raw = mutated()
    raw["pipelines"]["deals"][1]["label"] = "New Business"
    assert any("labels must be unique" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    raw["pipelines"]["deals"][0]["stages"] = raw["pipelines"]["deals"][0]["stages"][:-1]
    assert any("closed-lost" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    raw["pipelines"]["deals"][0]["stages"][0]["probability"] = 1.5
    assert any("less than or equal to 1" in p for p in problems_for(raw, tmp_path))


def test_custom_object_rules(tmp_path: Path) -> None:
    raw = mutated()
    raw["custom_objects"][0]["searchable"].append("ghost")
    assert any("undefined properties ['ghost']" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    for prop in raw["custom_objects"][0]["properties"]:
        prop.pop("unique", None)
    assert any("unique property to upsert by" in p for p in problems_for(raw, tmp_path))

    raw = mutated()
    raw["custom_objects"].append(copy.deepcopy(raw["custom_objects"][0]))
    assert any("names must be unique" in p for p in problems_for(raw, tmp_path))


def test_unique_property_cap(tmp_path: Path) -> None:
    raw = mutated()
    for i in range(10):
        raw["objects"]["contacts"]["properties"].append(
            {
                "name": f"ext_id_{i}",
                "label": "x",
                "type": "string",
                "field_type": "text",
                "unique": True,
            }
        )
    assert any("more than 10 unique" in p for p in problems_for(raw, tmp_path))


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    raw = mutated()
    raw["objects"]["contacts"]["properties"][0]["requird"] = True
    assert any("Extra inputs are not permitted" in p for p in problems_for(raw, tmp_path))


def test_field_dictionary_covers_everything() -> None:
    spec = PortalSpec.model_validate(RAW)
    doc = render_field_dictionary(spec, "portal/pipelineos.yaml")
    for fragment in [
        "`company_key`",
        "**Unique; upsert key.**",
        "REGEX `^ws_[a-z0-9]{6,}$`",
        "MIN_NUMBER 0",
        "## Custom object: Workspace (`workspace`)",
        "| `nb_proposal` | Proposal | 70% | no | `arr`, `amount` |",
        "| workspace | contacts | `workspace_admin` | Admin | Admin of |",
        "## Lifecycle stages",
        "## Manual steps (HubSpot UI only)",
    ]:
        assert fragment in doc, fragment


def test_field_dictionary_minimal_spec() -> None:
    spec = PortalSpec.model_validate(
        {
            "version": 1,
            "client": "X",
            "summary": "s",
            "objects": {
                "contacts": {
                    "properties": [
                        {
                            "name": "pipe_note",
                            "label": "A | B",
                            "type": "string",
                            "field_type": "text",
                        }
                    ]
                }
            },
        }
    )
    doc = render_field_dictionary(spec, "x.yaml")
    assert "A \\| B" in doc
    assert "## Lifecycle" not in doc
