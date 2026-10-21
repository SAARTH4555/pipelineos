"""In-memory HubSpot that enforces the rules this project depends on.

A fake that accepts anything would let the sync and apply code pass tests and then fail in a real
portal. So this one rejects what HubSpot rejects, in the places where it matters here:
unique-key upserts (companies cannot upsert by ``domain``), batch caps and duplicate keys,
properties that do not exist, enumeration values outside the option list, ``type``/``fieldType``
pairs, and the fields HubSpot never lets you change (a schema's ``name``, a property's
``hasUniqueValue``). Every call is recorded in ``calls`` so tests can assert on writes.
"""

from __future__ import annotations

import copy
import itertools
import re
from collections.abc import Sequence

from pipelineos.hubspot.client import (
    JSON,
    MAX_ASSOCIATION_BATCH,
    STANDARD_OBJECT_IDS,
    AssociationInput,
    BatchError,
    BatchResult,
    HubSpotError,
    RecordResult,
    UpsertInput,
    check_upsert_batch,
)

DEFAULT_PORTAL = 1234567

VALID_FIELD_TYPES: dict[str, frozenset[str]] = {
    "bool": frozenset({"booleancheckbox", "calculation_equation"}),
    "enumeration": frozenset(
        {"booleancheckbox", "checkbox", "radio", "select", "calculation_equation"}
    ),
    "date": frozenset({"date"}),
    "datetime": frozenset({"date"}),
    "string": frozenset(
        {"file", "text", "textarea", "calculation_equation", "html", "phonenumber"}
    ),
    "number": frozenset({"number", "calculation_equation"}),
}

_LIFECYCLE = ["subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead"]
_LIFECYCLE += ["opportunity", "customer", "evangelist", "other"]
_LEAD_STATUS = ["NEW", "OPEN", "IN_PROGRESS", "OPEN_DEAL", "UNQUALIFIED"]
_LEAD_STATUS += ["ATTEMPTED_TO_CONTACT", "CONNECTED", "BAD_TIMING"]
_INDUSTRY = ["COMPUTER_SOFTWARE", "FINANCIAL_SERVICES", "HOSPITAL_HEALTH_CARE", "INTERNET"]


def _builtin(
    name: str, group: str, type_: str, field_type: str, options: list[str] | None = None
) -> JSON:
    prop: JSON = {
        "name": name,
        "label": name,
        "groupName": group,
        "type": type_,
        "fieldType": field_type,
        "hasUniqueValue": False,
        "hubspotDefined": True,
    }
    if options is not None:
        prop["options"] = [{"label": o, "value": o} for o in options]
    return prop


def _seed_properties() -> dict[str, dict[str, JSON]]:
    c, co, d = "contactinformation", "companyinformation", "dealinformation"
    contacts = [
        _builtin("email", c, "string", "text"),
        _builtin("firstname", c, "string", "text"),
        _builtin("lastname", c, "string", "text"),
        _builtin("phone", c, "string", "phonenumber"),
        _builtin("company", c, "string", "text"),
        _builtin("jobtitle", c, "string", "text"),
        _builtin("lifecyclestage", c, "enumeration", "radio", _LIFECYCLE),
        _builtin("hs_lead_status", c, "enumeration", "radio", _LEAD_STATUS),
    ]
    companies = [
        _builtin("name", co, "string", "text"),
        _builtin("domain", co, "string", "text"),
        # HubSpot's industry is a fixed picklist (COMPUTER_SOFTWARE, ...), not free text.
        _builtin("industry", co, "enumeration", "select", _INDUSTRY),
        _builtin("numberofemployees", co, "number", "number"),
        _builtin("lifecyclestage", co, "enumeration", "radio", _LIFECYCLE),
    ]
    deals = [
        _builtin("dealname", d, "string", "text"),
        _builtin("amount", d, "number", "number"),
        _builtin("dealstage", d, "enumeration", "radio"),
        _builtin("pipeline", d, "enumeration", "select"),
        _builtin("closedate", d, "datetime", "date"),
    ]
    return {
        "contacts": {p["name"]: p for p in contacts},
        "companies": {p["name"]: p for p in companies},
        "deals": {p["name"]: p for p in deals},
    }


class FakeHubSpot:
    def __init__(self, portal: int = DEFAULT_PORTAL) -> None:
        self.portal = portal
        self.calls: list[tuple[str, str]] = []
        # Scripted failures: the next call pops and raises the first one.
        self.fail_next: list[HubSpotError] = []
        self.records: dict[str, dict[str, dict[str, str]]] = {t: {} for t in STANDARD_OBJECT_IDS}
        self.associations: set[tuple[str, str, str, str, int]] = set()
        self._ids = itertools.count(1001)
        self._groups: dict[str, dict[str, JSON]] = {
            "contacts": {"contactinformation": _group("contactinformation", "Contact information")},
            "companies": {
                "companyinformation": _group("companyinformation", "Company information")
            },
            "deals": {"dealinformation": _group("dealinformation", "Deal information")},
        }
        self._props = _seed_properties()
        self._schemas: dict[str, JSON] = {}
        self._pipelines: dict[str, dict[str, JSON]] = {"deals": {}}
        self._labels: dict[tuple[str, str], list[JSON]] = {}
        self._unique: dict[tuple[str, str], dict[str, str]] = {}
        self._rules: dict[str, dict[str, dict[str, JSON]]] = {}
        self._seed_default_pipeline()
        self._add_default_association("contacts", "companies", 279)
        self._add_default_association("deals", "companies", 341)
        self._add_default_association("deals", "contacts", 3)

    # -- helpers -----------------------------------------------------------------------------

    def _call(self, op: str, target: str) -> None:
        self.calls.append((op, target))
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _next_id(self) -> str:
        return str(next(self._ids))

    def _type(self, object_type: str) -> str:
        """Canonical key: a standard name (``contacts``) or a custom ``objectTypeId``."""
        if object_type in STANDARD_OBJECT_IDS:
            return object_type
        for name, type_id in STANDARD_OBJECT_IDS.items():
            if object_type == type_id:
                return name
        for type_id, schema in self._schemas.items():
            if object_type in (type_id, schema["fullyQualifiedName"]):
                return type_id
        raise HubSpotError(404, "OBJECT_NOT_FOUND", f"unknown object type {object_type!r}")

    def _add_default_association(self, a: str, b: str, type_id: int) -> None:
        unlabeled: JSON = {"category": "HUBSPOT_DEFINED", "typeId": type_id, "label": None}
        self._labels.setdefault((a, b), []).append(dict(unlabeled))
        self._labels.setdefault((b, a), []).append({**unlabeled, "typeId": type_id + 1})

    def _seed_default_pipeline(self) -> None:
        stages = [
            ("appointmentscheduled", "Appointment Scheduled", "0.2"),
            ("closedwon", "Closed Won", "1.0"),
            ("closedlost", "Closed Lost", "0.0"),
        ]
        self._pipelines["deals"]["default"] = {
            "id": "default",
            "label": "Sales Pipeline",
            "displayOrder": 0,
            "stages": [
                {"id": sid, "label": label, "displayOrder": i, "metadata": {"probability": p}}
                for i, (sid, label, p) in enumerate(stages)
            ],
        }

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if not c[0].startswith(("list", "get"))]

    # -- account -----------------------------------------------------------------------------

    def portal_id(self) -> int:
        self._call("get_portal", "")
        return self.portal

    # -- property groups and properties ------------------------------------------------------

    def list_property_groups(self, object_type: str) -> list[JSON]:
        self._call("list_groups", object_type)
        return copy.deepcopy(list(self._groups[self._type(object_type)].values()))

    def create_property_group(self, object_type: str, group: JSON) -> JSON:
        self._call("create_group", object_type)
        groups = self._groups[self._type(object_type)]
        if group["name"] in groups:
            raise HubSpotError(409, "CONFLICT", f"group {group['name']} already exists")
        groups[group["name"]] = _group(group["name"], group["label"], group.get("displayOrder"))
        return copy.deepcopy(groups[group["name"]])

    def update_property_group(self, object_type: str, name: str, patch: JSON) -> JSON:
        self._call("update_group", object_type)
        groups = self._groups[self._type(object_type)]
        if name not in groups:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"group {name} not found")
        groups[name].update({k: v for k, v in patch.items() if k in ("label", "displayOrder")})
        return copy.deepcopy(groups[name])

    def list_properties(self, object_type: str) -> list[JSON]:
        self._call("list_properties", object_type)
        return copy.deepcopy(list(self._props[self._type(object_type)].values()))

    def create_property(self, object_type: str, prop: JSON) -> JSON:
        self._call("create_property", object_type)
        t = self._type(object_type)
        self._register_property(t, prop)
        return copy.deepcopy(self._props[t][prop["name"]])

    def _register_property(self, t: str, prop: JSON) -> None:
        for key in ("groupName", "name", "label", "type", "fieldType"):
            if key not in prop:
                raise HubSpotError(400, "VALIDATION_ERROR", f"property is missing {key!r}")
        if prop["name"] in self._props[t]:
            raise HubSpotError(409, "CONFLICT", f"property {prop['name']} already exists")
        if prop["groupName"] not in self._groups[t]:
            raise HubSpotError(400, "VALIDATION_ERROR", f"group {prop['groupName']} not found")
        _check_field_type(prop)
        if prop.get("hasUniqueValue"):
            unique = [p for p in self._props[t].values() if p.get("hasUniqueValue")]
            if len(unique) >= 10:
                raise HubSpotError(400, "VALIDATION_ERROR", "object already has 10 unique props")
        stored = {"hasUniqueValue": False, "hubspotDefined": False, **copy.deepcopy(prop)}
        self._props[t][prop["name"]] = stored

    def update_property(self, object_type: str, name: str, patch: JSON) -> JSON:
        self._call("update_property", object_type)
        t = self._type(object_type)
        current = self._props[t].get(name)
        if current is None:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"property {name} not found")
        # HubSpot requires both on every update, not only on create.
        if "type" not in patch or "fieldType" not in patch:
            raise HubSpotError(400, "VALIDATION_ERROR", "type and fieldType are required")
        if "hasUniqueValue" in patch and patch["hasUniqueValue"] != current["hasUniqueValue"]:
            raise HubSpotError(400, "VALIDATION_ERROR", "hasUniqueValue cannot be changed")
        _check_field_type(patch)
        current.update(copy.deepcopy(patch))
        return copy.deepcopy(current)

    # -- custom object schemas ---------------------------------------------------------------

    def list_schemas(self) -> list[JSON]:
        self._call("list_schemas", "")
        return copy.deepcopy(list(self._schemas.values()))

    def create_schema(self, schema: JSON) -> JSON:
        self._call("create_schema", schema.get("name", ""))
        for key in ("name", "labels", "properties", "requiredProperties", "associatedObjects"):
            if key not in schema:
                raise HubSpotError(400, "VALIDATION_ERROR", f"schema is missing {key!r}")
        if any(s["name"] == schema["name"] for s in self._schemas.values()):
            raise HubSpotError(409, "CONFLICT", f"schema {schema['name']} already exists")
        type_id = f"2-{self._next_id()}"
        group = f"{schema['name']}_information"
        self._groups[type_id] = {group: _group(group, f"{schema['labels']['singular']} info")}
        self._props[type_id] = {}
        self.records[type_id] = {}
        for prop in schema["properties"]:
            self._register_property(
                type_id, {"groupName": group, "fieldType": "text", "type": "string", **prop}
            )
        prop_names = set(self._props[type_id])
        for ref in _schema_refs(schema):
            if ref not in prop_names:
                raise HubSpotError(400, "VALIDATION_ERROR", f"schema references unknown {ref}")
        stored: JSON = {
            "id": type_id.split("-")[1],
            "objectTypeId": type_id,
            "fullyQualifiedName": f"p{self.portal}_{schema['name']}",
            "name": schema["name"],
            "labels": dict(schema["labels"]),
            "primaryDisplayProperty": schema.get("primaryDisplayProperty"),
            "requiredProperties": list(schema["requiredProperties"]),
            "searchableProperties": list(schema.get("searchableProperties", [])),
            "secondaryDisplayProperties": list(schema.get("secondaryDisplayProperties", [])),
            "description": schema.get("description"),
            "associatedObjects": [self._type(o) for o in schema["associatedObjects"]],
        }
        self._schemas[type_id] = stored
        for other in stored["associatedObjects"]:
            type_id_forward = int(self._next_id())
            next(self._ids)  # the inverse direction takes forward + 1
            self._add_default_association(type_id, other, type_id_forward)
        return self._schema_view(type_id)

    def _schema_view(self, type_id: str) -> JSON:
        view = copy.deepcopy(self._schemas[type_id])
        view["properties"] = copy.deepcopy(list(self._props[type_id].values()))
        return view

    def update_schema(self, object_type_id: str, patch: JSON) -> JSON:
        self._call("update_schema", object_type_id)
        t = self._type(object_type_id)
        if t not in self._schemas:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"schema {object_type_id} not found")
        stored = self._schemas[t]
        if "name" in patch and patch["name"] != stored["name"]:
            raise HubSpotError(400, "VALIDATION_ERROR", "schema name cannot be changed")
        for ref in _schema_refs(patch):
            if ref not in self._props[t]:
                raise HubSpotError(400, "VALIDATION_ERROR", f"schema references unknown {ref}")
        allowed = (
            "labels",
            "primaryDisplayProperty",
            "requiredProperties",
            "searchableProperties",
            "secondaryDisplayProperties",
            "description",
        )
        stored.update({k: copy.deepcopy(v) for k, v in patch.items() if k in allowed})
        return self._schema_view(t)

    # -- pipelines ---------------------------------------------------------------------------

    def list_pipelines(self, object_type: str) -> list[JSON]:
        self._call("list_pipelines", object_type)
        return copy.deepcopy(list(self._pipelines.get(self._type(object_type), {}).values()))

    def create_pipeline(self, object_type: str, pipeline: JSON) -> JSON:
        self._call("create_pipeline", object_type)
        t = self._type(object_type)
        pipelines = self._pipelines.setdefault(t, {})
        if any(p["label"] == pipeline["label"] for p in pipelines.values()):
            raise HubSpotError(409, "CONFLICT", f"pipeline {pipeline['label']} already exists")
        pipeline_id = self._next_id()
        stored: JSON = {
            "id": pipeline_id,
            "label": pipeline["label"],
            "displayOrder": pipeline["displayOrder"],
            "stages": [],
        }
        pipelines[pipeline_id] = stored
        for stage in pipeline.get("stages", []):
            stored["stages"].append(self._make_stage(t, stage))
        return copy.deepcopy(stored)

    def _make_stage(self, t: str, stage: JSON) -> JSON:
        stage_id = stage.get("stageId") or self._next_id()
        if any(s["id"] == stage_id for p in self._pipelines[t].values() for s in p["stages"]):
            raise HubSpotError(409, "CONFLICT", f"stage id {stage_id} already used")
        metadata = {k: str(v) for k, v in (stage.get("metadata") or {}).items()}
        if t == "deals":
            _check_probability(metadata)
        return {
            "id": stage_id,
            "label": stage["label"],
            "displayOrder": stage["displayOrder"],
            "metadata": metadata,
        }

    def _pipeline(self, object_type: str, pipeline_id: str) -> tuple[str, JSON]:
        t = self._type(object_type)
        pipeline = self._pipelines.get(t, {}).get(pipeline_id)
        if pipeline is None:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"pipeline {pipeline_id} not found")
        return t, pipeline

    def update_pipeline(self, object_type: str, pipeline_id: str, patch: JSON) -> JSON:
        self._call("update_pipeline", object_type)
        _, pipeline = self._pipeline(object_type, pipeline_id)
        pipeline.update({k: v for k, v in patch.items() if k in ("label", "displayOrder")})
        return copy.deepcopy(pipeline)

    def create_stage(self, object_type: str, pipeline_id: str, stage: JSON) -> JSON:
        self._call("create_stage", object_type)
        t, pipeline = self._pipeline(object_type, pipeline_id)
        made = self._make_stage(t, stage)
        pipeline["stages"].append(made)
        return copy.deepcopy(made)

    def update_stage(self, object_type: str, pipeline_id: str, stage_id: str, patch: JSON) -> JSON:
        self._call("update_stage", object_type)
        t, pipeline = self._pipeline(object_type, pipeline_id)
        stage = next((s for s in pipeline["stages"] if s["id"] == stage_id), None)
        if stage is None:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"stage {stage_id} not found")
        if "metadata" in patch:
            metadata = {k: str(v) for k, v in patch["metadata"].items()}
            if t == "deals":
                _check_probability(metadata)
            stage["metadata"] = metadata
        stage.update({k: v for k, v in patch.items() if k in ("label", "displayOrder")})
        return copy.deepcopy(stage)

    # -- association labels ------------------------------------------------------------------

    def list_association_labels(self, from_type: str, to_type: str) -> list[JSON]:
        self._call("list_labels", f"{from_type}->{to_type}")
        key = (self._type(from_type), self._type(to_type))
        return copy.deepcopy(self._labels.get(key, []))

    def create_association_label(self, from_type: str, to_type: str, label: JSON) -> JSON:
        self._call("create_label", f"{from_type}->{to_type}")
        a, b = self._type(from_type), self._type(to_type)
        name = label["name"]
        if "-" in name or name[:1].isdigit():
            raise HubSpotError(400, "VALIDATION_ERROR", f"invalid label name {name!r}")
        existing = self._labels.setdefault((a, b), [])
        if any(x.get("name") == name for x in existing):
            raise HubSpotError(409, "CONFLICT", f"label {name} already exists")
        if sum(1 for x in existing if x["category"] == "USER_DEFINED") >= 10:
            raise HubSpotError(400, "VALIDATION_ERROR", "label limit reached for this pair")
        forward = int(self._next_id())
        inverse_label = label.get("inverseLabel") or label["label"]
        fwd: JSON = {
            "category": "USER_DEFINED",
            "typeId": forward,
            "label": label["label"],
            "name": name,
        }
        inv: JSON = {
            "category": "USER_DEFINED",
            "typeId": forward + 1,
            "label": inverse_label,
            "name": name,
        }
        existing.append(fwd)
        if (a, b) != (b, a):
            self._labels.setdefault((b, a), []).append(inv)
        next(self._ids)  # keep the inverse id from being reused
        return {"results": [copy.deepcopy(fwd), copy.deepcopy(inv)]}

    # -- property validation rules -----------------------------------------------------------

    def list_validation_rules(self, object_type_id: str) -> list[JSON]:
        self._call("list_rules", object_type_id)
        rules = self._rules.get(self._type(object_type_id), {})
        return [
            {"propertyName": prop, "ruleType": rule_type, **copy.deepcopy(body)}
            for prop, by_type in sorted(rules.items())
            for rule_type, body in sorted(by_type.items())
        ]

    def put_validation_rule(
        self, object_type_id: str, property_name: str, rule_type: str, body: JSON
    ) -> None:
        self._call("put_rule", object_type_id)
        t = self._type(object_type_id)
        if property_name not in self._props[t]:
            raise HubSpotError(404, "OBJECT_NOT_FOUND", f"property {property_name} not found")
        self._rules.setdefault(t, {}).setdefault(property_name, {})[rule_type] = {
            "ruleArguments": [str(a) for a in body.get("ruleArguments", [])],
            "shouldApplyNormalization": bool(body.get("shouldApplyNormalization", False)),
        }

    # -- records -----------------------------------------------------------------------------

    def _upsertable(self, t: str, id_property: str) -> bool:
        prop = self._props[t].get(id_property)
        if prop is None:
            return False
        return bool(prop.get("hasUniqueValue")) or (t == "contacts" and id_property == "email")

    def _index(self, t: str, prop: str) -> dict[str, str]:
        return self._unique.setdefault((t, prop), {})

    def batch_upsert(
        self, object_type: str, id_property: str, inputs: Sequence[UpsertInput]
    ) -> BatchResult:
        self._call("upsert", object_type)
        t = self._type(object_type)
        try:
            check_upsert_batch(inputs)
        except ValueError as exc:
            raise HubSpotError(400, "VALIDATION_ERROR", str(exc)) from exc
        if not self._upsertable(t, id_property):
            raise HubSpotError(
                400, "VALIDATION_ERROR", f"{id_property} is not a unique-value property on {t}"
            )
        unique_props = {n for n, p in self._props[t].items() if p.get("hasUniqueValue")}
        if t == "contacts":
            unique_props.add("email")
        result = BatchResult()
        for item in inputs:
            key = item.key.lower() if id_property == "email" else item.key
            props = {**item.properties, id_property: key}
            error = self._validate_values(t, props)
            existing = self._index(t, id_property).get(key)
            if error is None:
                error = self._unique_conflict(t, unique_props, props, existing)
            if error is not None:
                result.errors.append(BatchError(item.key, "VALIDATION_ERROR", error))
                continue
            record_id = existing or self._next_id()
            record = self.records[t].setdefault(record_id, {})
            record.update(props)
            for name in unique_props & props.keys():
                if props[name]:
                    self._index(t, name)[props[name]] = record_id
            result.results.append(RecordResult(item.key, record_id, created=existing is None))
        return result

    def _validate_values(self, t: str, props: dict[str, str]) -> str | None:
        for name, value in props.items():
            prop = self._props[t].get(name)
            if prop is None:
                return f"Property {name!r} does not exist"
            if value == "":
                continue
            if prop["type"] == "enumeration" and "options" in prop:
                allowed = {o["value"] for o in prop["options"]}
                bad = [v for v in value.split(";") if v and v not in allowed]
                if bad:
                    return f"{bad[0]!r} is not a valid option for {name!r}"
            elif prop["type"] == "number":
                try:
                    float(value)
                except ValueError:
                    return f"{value!r} is not a number for {name!r}"
            elif prop["type"] == "bool" and value not in ("true", "false"):
                return f"{value!r} is not a boolean for {name!r}"
            for rule_type, rule in self._rules.get(t, {}).get(name, {}).items():
                if not _passes_rule(rule_type, rule["ruleArguments"], value):
                    return f"{value!r} fails the {rule_type} validation rule on {name!r}"
        return None

    def _unique_conflict(
        self, t: str, unique_props: set[str], props: dict[str, str], record_id: str | None
    ) -> str | None:
        for name in unique_props & props.keys():
            owner = self._index(t, name).get(props[name])
            if props[name] and owner is not None and owner != record_id:
                return f"{name}={props[name]!r} already belongs to record {owner}"
        return None

    def batch_associate(
        self,
        from_type: str,
        to_type: str,
        type_id: int,
        category: str,
        pairs: Sequence[AssociationInput],
    ) -> int:
        self._call("associate", f"{from_type}->{to_type}")
        a, b = self._type(from_type), self._type(to_type)
        if len(pairs) > MAX_ASSOCIATION_BATCH:
            raise HubSpotError(400, "VALIDATION_ERROR", "association batch too large")
        known = {x["typeId"] for x in self._labels.get((a, b), [])}
        if type_id not in known:
            raise HubSpotError(400, "VALIDATION_ERROR", f"unknown association type {type_id}")
        for pair in pairs:
            if pair.from_id not in self.records[a] or pair.to_id not in self.records[b]:
                raise HubSpotError(404, "OBJECT_NOT_FOUND", f"record missing in {pair}")
            self.associations.add((a, pair.from_id, b, pair.to_id, type_id))
        return len(pairs)


_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _passes_rule(rule_type: str, args: list[str], value: str) -> bool:
    """The subset of HubSpot validation rules this project uses; unknown types pass."""
    checks = {
        "REGEX": lambda: re.search(args[0], value) is not None,
        "MIN_NUMBER": lambda: float(value) >= float(args[0]),
        "MAX_NUMBER": lambda: float(value) <= float(args[0]),
        "MIN_LENGTH": lambda: len(value) >= int(args[0]),
        "MAX_LENGTH": lambda: len(value) <= int(args[0]),
        "DOMAIN": lambda: _DOMAIN_RE.match(value) is not None,
        "EMAIL": lambda: _EMAIL_RE.match(value) is not None,
        "FORMAT": lambda: value == (value.lower() if args[0] == "LOWER" else value.upper()),
        "WHITESPACE": lambda: value == value.strip(),
    }
    check = checks.get(rule_type)
    return True if check is None else bool(check())


def _group(name: str, label: str, display_order: int | None = None) -> JSON:
    return {
        "name": name,
        "label": label,
        "displayOrder": -1 if display_order is None else display_order,
    }


def _check_field_type(prop: JSON) -> None:
    allowed = VALID_FIELD_TYPES.get(prop["type"])
    if allowed is None or prop["fieldType"] not in allowed:
        raise HubSpotError(
            400, "VALIDATION_ERROR", f"fieldType {prop['fieldType']} is invalid for {prop['type']}"
        )


def _check_probability(metadata: dict[str, str]) -> None:
    raw = metadata.get("probability")
    if raw is None:
        raise HubSpotError(400, "VALIDATION_ERROR", "deal stages need metadata.probability")
    if not 0.0 <= float(raw) <= 1.0:
        raise HubSpotError(400, "VALIDATION_ERROR", f"probability {raw} outside 0..1")


def _schema_refs(schema: JSON) -> list[str]:
    refs = list(schema.get("requiredProperties", []))
    refs += schema.get("searchableProperties", [])
    refs += schema.get("secondaryDisplayProperties", [])
    if schema.get("primaryDisplayProperty"):
        refs.append(schema["primaryDisplayProperty"])
    return refs
