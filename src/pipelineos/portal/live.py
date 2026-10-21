"""Read the parts of a live portal that the spec manages into one snapshot.

The planner is a pure function of (spec, snapshot), so all API reads happen here, up front, and
only for objects the spec mentions. A custom object whose schema does not exist yet simply has
no entry; the planner reads that as "create everything".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipelineos.hubspot.client import JSON, STANDARD_OBJECT_IDS, HubSpotClient
from pipelineos.spec.models import PortalSpec


@dataclass
class LiveObject:
    api_type: str  # what goes in API paths: "contacts", or a custom objectTypeId like "2-123"
    type_id: str  # numeric-style id for endpoints that want one: "0-1", "2-123"
    groups: dict[str, JSON] = field(default_factory=dict)
    properties: dict[str, JSON] = field(default_factory=dict)
    # property name -> rule type -> {"ruleArguments": [...], "shouldApplyNormalization": bool}
    rules: dict[str, dict[str, JSON]] = field(default_factory=dict)


@dataclass
class LiveState:
    objects: dict[str, LiveObject] = field(default_factory=dict)  # by spec object name
    schemas: dict[str, JSON] = field(default_factory=dict)  # every custom schema, by name
    pipelines: dict[str, list[JSON]] = field(default_factory=dict)  # by spec object name
    labels: dict[tuple[str, str], list[JSON]] = field(default_factory=dict)  # by spec names


def _read_object(client: HubSpotClient, api_type: str, type_id: str) -> LiveObject:
    obj = LiveObject(api_type=api_type, type_id=type_id)
    obj.groups = {g["name"]: g for g in client.list_property_groups(api_type)}
    obj.properties = {p["name"]: p for p in client.list_properties(api_type)}
    for row in client.list_validation_rules(type_id):
        body = {
            "ruleArguments": [str(a) for a in row.get("ruleArguments", [])],
            "shouldApplyNormalization": bool(row.get("shouldApplyNormalization", False)),
        }
        obj.rules.setdefault(str(row["propertyName"]), {})[str(row["ruleType"])] = body
    return obj


def read_live(client: HubSpotClient, spec: PortalSpec) -> LiveState:
    live = LiveState()
    for name in spec.objects:
        live.objects[name] = _read_object(client, name, STANDARD_OBJECT_IDS[name])

    live.schemas = {s["name"]: s for s in client.list_schemas()}
    for custom in spec.custom_objects:
        schema = live.schemas.get(custom.name)
        if schema is not None:
            type_id = str(schema["objectTypeId"])
            live.objects[custom.name] = _read_object(client, type_id, type_id)

    for object_name in spec.pipelines:
        live.pipelines[object_name] = client.list_pipelines(object_name)

    for label in spec.association_labels:
        pair = (label.from_object, label.to_object)
        if pair in live.labels:
            continue
        from_obj, to_obj = live.objects.get(pair[0]), live.objects.get(pair[1])
        from_type = from_obj.api_type if from_obj else _standard(pair[0])
        to_type = to_obj.api_type if to_obj else _standard(pair[1])
        if from_type is None or to_type is None:
            live.labels[pair] = []  # custom object not created yet: no labels can exist
            continue
        live.labels[pair] = client.list_association_labels(from_type, to_type)
    return live


def _standard(name: str) -> str | None:
    return name if name in STANDARD_OBJECT_IDS else None
