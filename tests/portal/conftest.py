from __future__ import annotations

import copy
from pathlib import Path

import pytest

from pipelineos.hubspot.client import STANDARD_OBJECT_IDS
from pipelineos.portal.live import LiveObject, LiveState
from pipelineos.portal.plan import (
    group_payload,
    pipeline_payload,
    property_payload,
    schema_payload,
)
from pipelineos.spec import PortalSpec, load_spec
from pipelineos.spec.models import DEFAULT_GROUPS

SPEC_PATH = Path(__file__).resolve().parents[2] / "portal" / "pipelineos.yaml"


@pytest.fixture
def spec() -> PortalSpec:
    return load_spec(SPEC_PATH)


def converged(spec: PortalSpec) -> LiveState:
    """A live snapshot that already matches the spec exactly, built from the payload builders."""
    live = LiveState()
    for name, obj in spec.objects.items():
        default = DEFAULT_GROUPS[name]
        lo = LiveObject(api_type=name, type_id=STANDARD_OBJECT_IDS[name])
        lo.groups = {default: {"name": default, "label": default, "displayOrder": -1}}
        lo.groups |= {g.name: group_payload(g) for g in obj.groups}
        lo.properties = {p.name: property_payload(p, p.group or default) for p in obj.properties}
        lo.properties["email"] = {"name": "email", "type": "string", "hubspotDefined": True}
        for p in obj.properties:
            for r in p.validation:
                lo.rules.setdefault(p.name, {})[r.rule] = {
                    "ruleArguments": list(r.arguments),
                    "shouldApplyNormalization": r.normalize,
                }
        live.objects[name] = lo
    for custom in spec.custom_objects:
        payload = schema_payload(custom)
        type_id = "2-900"
        schema = {k: copy.deepcopy(v) for k, v in payload.items() if k != "properties"}
        schema |= {"objectTypeId": type_id, "fullyQualifiedName": f"p1_{custom.name}"}
        live.schemas[custom.name] = schema
        group = f"{custom.name}_information"
        lo = LiveObject(api_type=type_id, type_id=type_id)
        lo.groups = {group: {"name": group, "label": "info"}}
        lo.properties = {p.name: property_payload(p, group) for p in custom.properties}
        for p in custom.properties:
            for r in p.validation:
                lo.rules.setdefault(p.name, {})[r.rule] = {
                    "ruleArguments": list(r.arguments),
                    "shouldApplyNormalization": r.normalize,
                }
        live.objects[custom.name] = lo
    for object_name, pipelines in spec.pipelines.items():
        rows = []
        for i, pipeline in enumerate(pipelines):
            payload = pipeline_payload(pipeline)
            stages = [
                {"id": s["stageId"], **{k: v for k, v in s.items() if k != "stageId"}}
                for s in payload["stages"]
            ]
            rows.append({**payload, "id": f"p{i}", "stages": stages})
        live.pipelines[object_name] = rows
    for label in spec.association_labels:
        live.labels.setdefault((label.from_object, label.to_object), []).append(
            {"category": "USER_DEFINED", "typeId": 1, "label": label.label}
        )
    return live
