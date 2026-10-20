"""Diff the spec against a live snapshot into an ordered list of actions.

``build_plan`` is pure: no API calls, O(n) in spec size via dict lookups on stable keys
(property names, schema names, pipeline labels, stage ids, label text, rule types).

It never plans a delete. Anything live that the spec does not mention is reported as
``UNMANAGED`` and left alone. Changes HubSpot will not make in place (a property's ``type`` or
``hasUniqueValue``, a custom object's labels or associated objects) are reported as ``MANUAL``
with the reason, instead of being "fixed" by deleting and recreating data.

Actions come out in dependency order: groups before the properties that live in them, schema
creation before anything on the custom object, new properties before a schema update that
references them, and properties before their validation rules.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pipelineos.hubspot.client import JSON, STANDARD_OBJECT_IDS
from pipelineos.portal.live import LiveObject, LiveState
from pipelineos.spec.models import (
    DEFAULT_GROUPS,
    CustomObjectSpec,
    GroupSpec,
    PipelineSpec,
    PortalSpec,
    PropertySpec,
    StageSpec,
)

Kind = Literal["group", "property", "schema", "validation", "pipeline", "stage", "label"]


class Op(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    MANUAL = "manual"
    UNMANAGED = "unmanaged"


@dataclass(frozen=True)
class Action:
    op: Op
    kind: Kind
    object_ref: str  # spec object name: contacts / companies / deals / a custom object name
    name: str  # group, property, schema, pipeline label, stage id, label text or rule type
    detail: str = ""
    payload: JSON = field(default_factory=dict, compare=False)
    parent: str = ""  # property (validation), pipeline label (stage), target object (label)
    live_id: str = ""  # pipeline id, when the pipeline already exists

    @property
    def target(self) -> str:
        if self.kind == "validation":
            return f"{self.object_ref}.{self.parent} {self.name}"
        if self.kind == "stage":
            return f"{self.object_ref} / {self.parent} / {self.name}"
        if self.kind == "label":
            return f"{self.object_ref} -> {self.parent}: {self.name}"
        if self.kind in ("schema", "pipeline"):
            return f"{self.object_ref}: {self.name}" if self.kind == "pipeline" else self.name
        return f"{self.object_ref}.{self.name}"


@dataclass
class Plan:
    actions: list[Action]

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.op in (Op.CREATE, Op.UPDATE)]

    @property
    def manual(self) -> list[Action]:
        return [a for a in self.actions if a.op is Op.MANUAL]

    def counts(self) -> dict[Op, int]:
        counted = Counter(a.op for a in self.actions)
        return {op: counted.get(op, 0) for op in Op}


# -- payload builders (spec -> HubSpot JSON) --------------------------------------------------


def property_payload(prop: PropertySpec, group: str) -> JSON:
    payload: JSON = {
        "name": prop.name,
        "label": prop.label,
        "description": prop.description,
        "groupName": group,
        "type": prop.type,
        "fieldType": prop.field_type,
        "hasUniqueValue": prop.unique,
    }
    if prop.options is not None:
        payload["options"] = [
            {"label": o.label, "value": o.value, "displayOrder": i}
            for i, o in enumerate(prop.options)
        ]
    elif prop.type == "bool":
        # HubSpot's single-checkbox properties carry explicit true/false options.
        payload["options"] = [
            {"label": "Yes", "value": "true", "displayOrder": 0},
            {"label": "No", "value": "false", "displayOrder": 1},
        ]
    return payload


def group_payload(group: GroupSpec) -> JSON:
    return {"name": group.name, "label": group.label, "displayOrder": group.display_order}


def schema_payload(custom: CustomObjectSpec) -> JSON:
    return {
        "name": custom.name,
        "labels": {"singular": custom.singular, "plural": custom.plural},
        "description": custom.description,
        "primaryDisplayProperty": custom.primary_display,
        "secondaryDisplayProperties": list(custom.secondary_display),
        "requiredProperties": list(custom.required),
        "searchableProperties": list(custom.searchable),
        "associatedObjects": [STANDARD_OBJECT_IDS[o] for o in custom.associated],
        "allowsSensitiveProperties": False,
        "shouldCreateSameObjectAssociation": False,
        "properties": [
            {k: v for k, v in property_payload(p, "").items() if k != "groupName"}
            for p in custom.properties
        ],
    }


def stage_payload(stage: StageSpec, order: int) -> JSON:
    return {
        "stageId": stage.id,
        "label": stage.label,
        "displayOrder": order,
        "metadata": {
            "probability": str(stage.probability),
            "isClosed": "true" if stage.closed else "false",
        },
    }


def pipeline_payload(pipeline: PipelineSpec) -> JSON:
    return {
        "label": pipeline.label,
        "displayOrder": pipeline.display_order,
        "stages": [stage_payload(s, i) for i, s in enumerate(pipeline.stages)],
    }


# -- diff helpers ------------------------------------------------------------------------------


def _changed(field_name: str, live: Any, desired: Any) -> str:
    return f"{field_name}: {live!r} -> {desired!r}"


def _options(prop: JSON) -> list[tuple[str, str]]:
    options = sorted(prop.get("options") or [], key=lambda o: o.get("displayOrder", 0))
    return [(str(o["value"]), str(o["label"])) for o in options]


def diff_property(desired: JSON, live: JSON) -> tuple[Op, list[str]]:
    if live.get("hubspotDefined"):
        return Op.MANUAL, ["name collides with a HubSpot-defined property; rename it in the spec"]
    if live.get("type") != desired["type"]:
        return Op.MANUAL, [
            _changed("type", live.get("type"), desired["type"])
            + " (changing a property's type needs delete and recreate)"
        ]
    if bool(live.get("hasUniqueValue")) != desired["hasUniqueValue"]:
        return Op.MANUAL, ["hasUniqueValue can never change after creation"]
    changes = [
        _changed(f, live.get(f) or "", desired[f])
        for f in ("label", "description", "groupName", "fieldType")
        if (live.get(f) or "") != desired[f]
    ]
    if desired["type"] == "enumeration" and _options(live) != _options(desired):
        live_values = [v for v, _ in _options(live)]
        desired_values = [v for v, _ in _options(desired)]
        removed = [v for v in live_values if v not in desired_values]
        note = f" (removes {removed}: existing values stay but become invalid)" if removed else ""
        changes.append(f"options: {live_values} -> {desired_values}{note}")
    return (Op.UPDATE if changes else Op.NOOP), changes


def _live_associated(schema: JSON) -> set[str]:
    if "associatedObjects" in schema:
        return {_object_name(str(o)) for o in schema["associatedObjects"]}
    return {
        _object_name(str(a.get("toObjectTypeId", "")))
        for a in schema.get("associations", [])
        if a.get("fromObjectTypeId") == schema.get("objectTypeId")
    }


def _object_name(ref: str) -> str:
    for name, type_id in STANDARD_OBJECT_IDS.items():
        if ref == type_id:
            return name
    return ref


def _default_group(obj: LiveObject, custom_name: str) -> str:
    preferred = f"{custom_name}_information"
    if preferred in obj.groups or not obj.groups:
        return preferred
    return next(iter(obj.groups))


# -- planner ----------------------------------------------------------------------------------


class _Planner:
    def __init__(self, spec: PortalSpec, live: LiveState) -> None:
        self.spec = spec
        self.live = live
        self.actions: list[Action] = []
        self.unmanaged: list[Action] = []

    def add(self, action: Action) -> None:
        (self.unmanaged if action.op is Op.UNMANAGED else self.actions).append(action)

    def run(self) -> Plan:
        for name, obj in self.spec.objects.items():
            self.groups(name, obj.groups)
            self.properties(name, obj.properties, lambda p, n=name: p.group or DEFAULT_GROUPS[n])
        for custom in self.spec.custom_objects:
            if custom.name not in self.live.objects:
                self.add(
                    Action(
                        Op.CREATE,
                        "schema",
                        custom.name,
                        custom.name,
                        f"{len(custom.properties)} properties",
                        schema_payload(custom),
                    )
                )
        for custom in self.spec.custom_objects:
            if custom.name in self.live.objects:
                self.existing_schema(custom)
        for name, obj in self.spec.objects.items():
            self.validation(name, obj.properties)
        for custom in self.spec.custom_objects:
            self.validation(custom.name, custom.properties)
        for object_name, pipelines in self.spec.pipelines.items():
            self.pipelines(object_name, pipelines)
        self.labels()
        self.unmanaged_schemas()
        return Plan(self.actions + self.unmanaged)

    # groups and properties

    def groups(self, object_name: str, groups: list[GroupSpec]) -> None:
        live = self.live.objects[object_name].groups
        for group in groups:
            current = live.get(group.name)
            if current is None:
                self.add(
                    Action(
                        Op.CREATE,
                        "group",
                        object_name,
                        group.name,
                        group.label,
                        group_payload(group),
                    )
                )
            elif current.get("label") != group.label or (
                group.display_order != -1 and current.get("displayOrder") != group.display_order
            ):
                self.add(
                    Action(
                        Op.UPDATE,
                        "group",
                        object_name,
                        group.name,
                        _changed("label", current.get("label"), group.label),
                        group_payload(group),
                    )
                )
            else:
                self.add(Action(Op.NOOP, "group", object_name, group.name))
        wanted = {g.name for g in groups} | {DEFAULT_GROUPS.get(object_name, "")}
        for name in sorted(set(live) - wanted):
            if not name.endswith("information"):
                self.add(Action(Op.UNMANAGED, "group", object_name, name, "not in spec"))

    def properties(self, object_name: str, props: list[PropertySpec], group_for: Any) -> None:
        live = self.live.objects[object_name].properties
        for prop in props:
            desired = property_payload(prop, group_for(prop))
            current = live.get(prop.name)
            if current is None:
                self.add(
                    Action(
                        Op.CREATE,
                        "property",
                        object_name,
                        prop.name,
                        f"{prop.type}/{prop.field_type}",
                        desired,
                    )
                )
                continue
            op, changes = diff_property(desired, current)
            self.add(Action(op, "property", object_name, prop.name, "; ".join(changes), desired))
        wanted = {p.name for p in props}
        for name, current in sorted(live.items()):
            if name not in wanted and not current.get("hubspotDefined"):
                self.add(Action(Op.UNMANAGED, "property", object_name, name, "not in spec"))

    # custom objects

    def existing_schema(self, custom: CustomObjectSpec) -> None:
        obj = self.live.objects[custom.name]
        default_group = _default_group(obj, custom.name)
        self.properties(
            custom.name,
            custom.properties,
            lambda p: p.group or obj.properties.get(p.name, {}).get("groupName") or default_group,
        )
        schema = self.live.schemas[custom.name]
        labels = schema.get("labels") or {}
        if (labels.get("singular"), labels.get("plural")) != (custom.singular, custom.plural):
            self.add(
                Action(
                    Op.MANUAL,
                    "schema",
                    custom.name,
                    custom.name,
                    "object labels cannot change after creation",
                )
            )
        if _live_associated(schema) != set(custom.associated):
            self.add(
                Action(
                    Op.MANUAL,
                    "schema",
                    custom.name,
                    custom.name,
                    "associated objects changed: add the association in Settings",
                )
            )
        desired = schema_payload(custom)
        patch: JSON = {}
        changes: list[str] = []
        for key in ("description", "primaryDisplayProperty"):
            if (schema.get(key) or "") != desired[key]:
                patch[key] = desired[key]
                changes.append(_changed(key, schema.get(key), desired[key]))
        for key in ("requiredProperties", "searchableProperties"):
            if set(schema.get(key) or []) != set(desired[key]):
                patch[key] = desired[key]
                changes.append(_changed(key, sorted(schema.get(key) or []), sorted(desired[key])))
        if (
            list(schema.get("secondaryDisplayProperties") or [])
            != desired["secondaryDisplayProperties"]
        ):
            patch["secondaryDisplayProperties"] = desired["secondaryDisplayProperties"]
            changes.append(
                _changed(
                    "secondaryDisplayProperties",
                    schema.get("secondaryDisplayProperties"),
                    desired["secondaryDisplayProperties"],
                )
            )
        op = Op.UPDATE if patch else Op.NOOP
        self.add(Action(op, "schema", custom.name, custom.name, "; ".join(changes), patch))

    def unmanaged_schemas(self) -> None:
        wanted = {c.name for c in self.spec.custom_objects}
        for name in sorted(set(self.live.schemas) - wanted):
            self.add(Action(Op.UNMANAGED, "schema", name, name, "not in spec"))

    # validation rules

    def validation(self, object_name: str, props: list[PropertySpec]) -> None:
        obj = self.live.objects.get(object_name)
        live_rules = obj.rules if obj else {}
        for prop in props:
            current = live_rules.get(prop.name, {})
            for rule in prop.validation:
                desired: JSON = {
                    "ruleArguments": list(rule.arguments),
                    "shouldApplyNormalization": rule.normalize,
                }
                existing = current.get(rule.rule)
                if existing is None:
                    op, detail = Op.CREATE, " ".join(rule.arguments[:1])
                elif existing != desired:
                    op = Op.UPDATE
                    detail = _changed("ruleArguments", existing["ruleArguments"], rule.arguments)
                else:
                    op, detail = Op.NOOP, ""
                self.add(
                    Action(
                        op, "validation", object_name, rule.rule, detail, desired, parent=prop.name
                    )
                )
            for rule_type in sorted(set(current) - {r.rule for r in prop.validation}):
                self.add(
                    Action(
                        Op.UNMANAGED,
                        "validation",
                        object_name,
                        rule_type,
                        "not in spec",
                        parent=prop.name,
                    )
                )

    # pipelines

    def pipelines(self, object_name: str, pipelines: list[PipelineSpec]) -> None:
        live = self.live.pipelines.get(object_name, [])
        by_label = {p["label"]: p for p in live}
        stage_home = {s["id"]: p["label"] for p in live for s in p.get("stages", [])}
        for pipeline in pipelines:
            current = by_label.get(pipeline.label)
            if current is None:
                clashes = [s.id for s in pipeline.stages if s.id in stage_home]
                if clashes:
                    self.add(
                        Action(
                            Op.MANUAL,
                            "pipeline",
                            object_name,
                            pipeline.label,
                            f"stage ids {clashes} already used by another pipeline",
                        )
                    )
                    continue
                self.add(
                    Action(
                        Op.CREATE,
                        "pipeline",
                        object_name,
                        pipeline.label,
                        f"{len(pipeline.stages)} stages",
                        pipeline_payload(pipeline),
                    )
                )
                continue
            pipeline_id = str(current["id"])
            if current.get("displayOrder") != pipeline.display_order:
                self.add(
                    Action(
                        Op.UPDATE,
                        "pipeline",
                        object_name,
                        pipeline.label,
                        _changed(
                            "displayOrder", current.get("displayOrder"), pipeline.display_order
                        ),
                        {"displayOrder": pipeline.display_order},
                        live_id=pipeline_id,
                    )
                )
            else:
                self.add(
                    Action(Op.NOOP, "pipeline", object_name, pipeline.label, live_id=pipeline_id)
                )
            self.stages(object_name, pipeline, current, stage_home)
        wanted = {p.label for p in pipelines}
        for label in sorted(set(by_label) - wanted):
            self.add(Action(Op.UNMANAGED, "pipeline", object_name, label, "not in spec"))

    def stages(
        self, object_name: str, pipeline: PipelineSpec, current: JSON, stage_home: dict[str, str]
    ) -> None:
        pipeline_id = str(current["id"])
        live_stages = {s["id"]: s for s in current.get("stages", [])}
        for order, stage in enumerate(pipeline.stages):
            desired = stage_payload(stage, order)
            live = live_stages.get(stage.id)
            if live is None:
                home = stage_home.get(stage.id)
                if home is not None:
                    self.add(
                        Action(
                            Op.MANUAL,
                            "stage",
                            object_name,
                            stage.id,
                            f"stage id already used by pipeline {home!r}",
                            parent=pipeline.label,
                            live_id=pipeline_id,
                        )
                    )
                    continue
                self.add(
                    Action(
                        Op.CREATE,
                        "stage",
                        object_name,
                        stage.id,
                        stage.label,
                        desired,
                        parent=pipeline.label,
                        live_id=pipeline_id,
                    )
                )
                continue
            changes = _stage_changes(live, desired)
            op = Op.UPDATE if changes else Op.NOOP
            patch = {k: v for k, v in desired.items() if k != "stageId"}
            self.add(
                Action(
                    op,
                    "stage",
                    object_name,
                    stage.id,
                    "; ".join(changes),
                    patch,
                    parent=pipeline.label,
                    live_id=pipeline_id,
                )
            )
        for stage_id in sorted(set(live_stages) - {s.id for s in pipeline.stages}):
            self.add(
                Action(
                    Op.UNMANAGED,
                    "stage",
                    object_name,
                    stage_id,
                    "not in spec",
                    parent=pipeline.label,
                    live_id=pipeline_id,
                )
            )

    # association labels

    def labels(self) -> None:
        for label in self.spec.association_labels:
            pair = (label.from_object, label.to_object)
            live = self.live.labels.get(pair, [])
            if any(x.get("label") == label.label for x in live):
                self.add(
                    Action(Op.NOOP, "label", label.from_object, label.label, parent=label.to_object)
                )
                continue
            payload: JSON = {"label": label.label, "name": label.name}
            if label.inverse_label:
                payload["inverseLabel"] = label.inverse_label
            self.add(
                Action(
                    Op.CREATE,
                    "label",
                    label.from_object,
                    label.label,
                    label.name,
                    payload,
                    parent=label.to_object,
                )
            )
        for pair, live in sorted(self.live.labels.items()):
            wanted = {
                x.label
                for x in self.spec.association_labels
                if (x.from_object, x.to_object) == pair
            }
            for row in live:
                if row.get("category") == "USER_DEFINED" and row.get("label") not in wanted:
                    self.add(
                        Action(
                            Op.UNMANAGED,
                            "label",
                            pair[0],
                            str(row.get("label")),
                            "not in spec",
                            parent=pair[1],
                        )
                    )


def _stage_changes(live: JSON, desired: JSON) -> list[str]:
    changes: list[str] = []
    if live.get("label") != desired["label"]:
        changes.append(_changed("label", live.get("label"), desired["label"]))
    if live.get("displayOrder") != desired["displayOrder"]:
        changes.append(_changed("displayOrder", live.get("displayOrder"), desired["displayOrder"]))
    metadata = live.get("metadata") or {}
    live_probability = float(metadata.get("probability", "nan"))
    desired_probability = float(desired["metadata"]["probability"])
    if live_probability != desired_probability:
        changes.append(
            _changed("probability", metadata.get("probability"), desired["metadata"]["probability"])
        )
    live_closed = str(metadata.get("isClosed", "false")).lower()
    if live_closed != desired["metadata"]["isClosed"]:
        changes.append(_changed("isClosed", live_closed, desired["metadata"]["isClosed"]))
    return changes


def build_plan(spec: PortalSpec, live: LiveState) -> Plan:
    return _Planner(spec, live).run()
