"""Typed model of the portal spec (``portal/*.yaml``).

The spec is the source of truth for the HubSpot portal: groups, properties and their validation
rules, the custom ``Workspace`` object, deal pipelines, association labels, lifecycle definitions
and the steps HubSpot only allows in its UI. Validation happens here, before any API call, so a
typo fails in milliseconds with a precise message instead of as a 400 halfway through an apply.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StandardObject = Literal["contacts", "companies", "deals"]
PropertyType = Literal["string", "number", "bool", "enumeration", "date", "datetime"]
FieldType = Literal[
    "text",
    "textarea",
    "phonenumber",
    "html",
    "file",
    "number",
    "booleancheckbox",
    "checkbox",
    "radio",
    "select",
    "date",
]

VALID_FIELD_TYPES: dict[str, frozenset[str]] = {
    "bool": frozenset({"booleancheckbox"}),
    "enumeration": frozenset({"booleancheckbox", "checkbox", "radio", "select"}),
    "date": frozenset({"date"}),
    "datetime": frozenset({"date"}),
    "string": frozenset({"file", "text", "textarea", "html", "phonenumber"}),
    "number": frozenset({"number"}),
}

# Rule types the HubSpot property-validations API accepts that this project uses.
RuleType = Literal[
    "EMAIL",
    "DOMAIN",
    "URL",
    "REGEX",
    "MIN_LENGTH",
    "MAX_LENGTH",
    "MIN_NUMBER",
    "MAX_NUMBER",
    "DECIMAL",
    "FORMAT",
    "WHITESPACE",
    "ALPHANUMERIC",
]

DEFAULT_GROUPS: dict[str, str] = {
    "contacts": "contactinformation",
    "companies": "companyinformation",
    "deals": "dealinformation",
}

# Built-in deal properties a stage may require without the spec defining them.
BUILTIN_DEAL_PROPERTIES = frozenset({"dealname", "amount", "closedate", "hubspot_owner_id"})

# snake_case, starts with a letter, and never in HubSpot's reserved ``hs_`` namespace.
NAME_RE = re.compile(r"^(?!hs_)[a-z][a-z0-9_]{1,99}$")
MAX_UNIQUE_PROPERTIES = 10


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_name(value: str, what: str) -> str:
    if not NAME_RE.match(value):
        raise ValueError(
            f"{what} {value!r} must be snake_case, start with a letter and not use the hs_ prefix"
        )
    return value


class Option(_Model):
    value: str
    label: str

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data: object) -> object:
        # Lets the YAML say ``options: [free, team]`` instead of spelling out every label.
        if isinstance(data, str):
            return {"value": data, "label": data.replace("_", " ").capitalize()}
        return data


class ValidationRule(_Model):
    rule: RuleType
    arguments: list[str] = Field(default_factory=list)
    normalize: bool = False


class PropertySpec(_Model):
    name: str
    label: str
    type: PropertyType
    field_type: FieldType
    group: str | None = None
    description: str = ""
    unique: bool = False
    options: list[Option] | None = None
    validation: list[ValidationRule] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _check_name(value, "property name")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.field_type not in VALID_FIELD_TYPES[self.type]:
            raise ValueError(
                f"{self.name}: field_type {self.field_type!r} is not valid for type {self.type!r}"
            )
        if self.type == "enumeration" and not self.options:
            raise ValueError(f"{self.name}: enumeration properties need options")
        if self.type != "enumeration" and self.options is not None:
            raise ValueError(f"{self.name}: only enumeration properties take options")
        if self.options is not None:
            values = [o.value for o in self.options]
            if len(values) != len(set(values)):
                raise ValueError(f"{self.name}: duplicate option values")
        if self.unique and self.type != "string":
            raise ValueError(f"{self.name}: unique properties must be strings")
        rules = [r.rule for r in self.validation]
        if len(rules) != len(set(rules)):
            raise ValueError(f"{self.name}: one rule per rule type")
        return self


class GroupSpec(_Model):
    name: str
    label: str
    display_order: int = -1

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _check_name(value, "group name")


class ObjectSpec(_Model):
    groups: list[GroupSpec] = Field(default_factory=list)
    properties: list[PropertySpec] = Field(default_factory=list)


class CustomObjectSpec(_Model):
    name: str
    singular: str
    plural: str
    description: str = ""
    primary_display: str
    secondary_display: list[str] = Field(default_factory=list)
    required: list[str]
    searchable: list[str] = Field(default_factory=list)
    associated: list[StandardObject]
    properties: list[PropertySpec]

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _check_name(value, "custom object name")

    @model_validator(mode="after")
    def _refs(self) -> Self:
        names = {p.name for p in self.properties}
        refs = [self.primary_display, *self.secondary_display, *self.required, *self.searchable]
        missing = sorted({r for r in refs if r not in names})
        if missing:
            raise ValueError(f"{self.name}: references undefined properties {missing}")
        if not any(p.unique for p in self.properties):
            raise ValueError(f"{self.name}: needs one unique property to upsert by")
        return self


class StageSpec(_Model):
    id: str
    label: str
    probability: float = Field(ge=0.0, le=1.0)
    closed: bool = False
    required_properties: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_name(value, "stage id")


class PipelineSpec(_Model):
    label: str
    display_order: int
    stages: list[StageSpec]

    @model_validator(mode="after")
    def _closed_stages(self) -> Self:
        closed = [s for s in self.stages if s.closed]
        if not any(s.probability == 1.0 for s in closed) or not any(
            s.probability == 0.0 for s in closed
        ):
            raise ValueError(f"pipeline {self.label!r} needs a closed-won and a closed-lost stage")
        return self


class AssociationLabelSpec(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_object: str = Field(alias="from")
    to_object: str = Field(alias="to")
    name: str
    label: str
    inverse_label: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        # HubSpot rejects hyphens and a leading digit; snake_case covers both.
        return _check_name(value, "association label name")


class LifecycleStageSpec(_Model):
    stage: str
    definition: str
    entry: str
    owner: str


class ManualStep(_Model):
    area: str
    step: str
    why: str


class PortalSpec(_Model):
    version: Literal[1]
    client: str
    summary: str
    objects: dict[StandardObject, ObjectSpec]
    custom_objects: list[CustomObjectSpec] = Field(default_factory=list)
    pipelines: dict[Literal["deals"], list[PipelineSpec]] = Field(default_factory=dict)
    association_labels: list[AssociationLabelSpec] = Field(default_factory=list)
    lifecycle: list[LifecycleStageSpec] = Field(default_factory=list)
    manual: list[ManualStep] = Field(default_factory=list)

    def object_names(self) -> set[str]:
        return {"contacts", "companies", "deals"} | {c.name for c in self.custom_objects}

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        errors: list[str] = []
        for object_name, obj in self.objects.items():
            errors += _property_checks(object_name, obj.properties, obj.groups)
        for custom in self.custom_objects:
            errors += _property_checks(custom.name, custom.properties, [])
        if len({c.name for c in self.custom_objects}) != len(self.custom_objects):
            errors.append("custom object names must be unique")

        deal_props = {p.name for p in self.objects.get("deals", ObjectSpec()).properties}
        stage_ids: set[str] = set()
        for pipelines in self.pipelines.values():
            labels = [p.label for p in pipelines]
            if len(labels) != len(set(labels)):
                errors.append("pipeline labels must be unique (pipelines are matched by label)")
            for pipeline in pipelines:
                for stage in pipeline.stages:
                    if stage.id in stage_ids:
                        errors.append(f"stage id {stage.id!r} is used twice")
                    stage_ids.add(stage.id)
                    unknown = set(stage.required_properties) - deal_props - BUILTIN_DEAL_PROPERTIES
                    if unknown:
                        errors.append(
                            f"stage {stage.id!r} requires undefined properties {sorted(unknown)}"
                        )

        known = self.object_names()
        seen_labels: set[tuple[str, str, str]] = set()
        for label in self.association_labels:
            for ref in (label.from_object, label.to_object):
                if ref not in known:
                    errors.append(f"association label {label.name!r} references unknown {ref!r}")
            key = (label.from_object, label.to_object, label.name)
            if key in seen_labels:
                errors.append(f"association label {label.name!r} is defined twice")
            seen_labels.add(key)

        if errors:
            raise ValueError("; ".join(errors))
        return self


def _property_checks(
    object_name: str, properties: list[PropertySpec], groups: list[GroupSpec]
) -> list[str]:
    errors: list[str] = []
    names = [p.name for p in properties]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"{object_name}: duplicate properties {dupes}")
    group_names = {g.name for g in groups}
    if object_name in DEFAULT_GROUPS:
        group_names.add(DEFAULT_GROUPS[object_name])
    for prop in properties:
        if prop.group is not None and prop.group not in group_names:
            errors.append(f"{object_name}.{prop.name}: unknown group {prop.group!r}")
    if sum(p.unique for p in properties) > MAX_UNIQUE_PROPERTIES:
        errors.append(f"{object_name}: more than {MAX_UNIQUE_PROPERTIES} unique properties")
    return errors
