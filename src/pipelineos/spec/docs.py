"""Render the field dictionary (``docs/field-dictionary.md``) from the spec.

A hand-written field dictionary drifts from the portal within a week. Generating it from the same
YAML that ``apply`` uses, and failing CI when the committed copy is stale, keeps the two equal.
"""

from __future__ import annotations

from pipelineos.spec.models import PortalSpec, PropertySpec, ValidationRule

TITLES = {"contacts": "Contacts", "companies": "Companies", "deals": "Deals"}


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _rule(rule: ValidationRule) -> str:
    if rule.rule == "REGEX" and rule.arguments:
        return f"REGEX `{rule.arguments[0]}`"
    if rule.arguments:
        return f"{rule.rule} {', '.join(rule.arguments)}"
    return rule.rule


def _property_rows(properties: list[PropertySpec], default_group: str) -> list[str]:
    rows = [
        "| Property | Label | Type | Group | Notes |",
        "|---|---|---|---|---|",
    ]
    for prop in properties:
        notes: list[str] = []
        if prop.unique:
            notes.append("**Unique; upsert key.**")
        if prop.description:
            notes.append(_cell(prop.description))
        if prop.options:
            notes.append("Options: " + ", ".join(f"`{o.value}`" for o in prop.options) + ".")
        if prop.validation:
            notes.append("Validation: " + "; ".join(_rule(r) for r in prop.validation) + ".")
        rows.append(
            f"| `{prop.name}` | {_cell(prop.label)} | {prop.type} / {prop.field_type} | "
            f"{prop.group or default_group} | {' '.join(notes)} |"
        )
    return rows


def render_field_dictionary(spec: PortalSpec, source: str) -> str:
    out: list[str] = [
        f"# Field dictionary: {spec.client}",
        "",
        f"Generated from `{source}` by `pipelineos dictionary`. Do not edit by hand.",
        "",
        _cell(spec.summary),
        "",
    ]
    for name, obj in spec.objects.items():
        out += [f"## {TITLES[name]}", ""]
        if obj.groups:
            out += ["Property groups: " + ", ".join(f"`{g.name}` ({g.label})" for g in obj.groups)]
            out += [""]
        out += _property_rows(obj.properties, "default")
        out += [""]

    for custom in spec.custom_objects:
        out += [f"## Custom object: {custom.singular} (`{custom.name}`)", ""]
        if custom.description:
            out += [_cell(custom.description), ""]
        out += [
            f"- Primary display: `{custom.primary_display}`",
            f"- Required: {', '.join(f'`{p}`' for p in custom.required)}",
            f"- Searchable: {', '.join(f'`{p}`' for p in custom.searchable) or 'none'}",
            f"- Associated with: {', '.join(custom.associated)}",
            "",
        ]
        out += _property_rows(custom.properties, f"{custom.name}_information")
        out += [""]

    for object_name, pipelines in spec.pipelines.items():
        for pipeline in pipelines:
            out += [f"## Pipeline: {pipeline.label} ({object_name})", ""]
            out += [
                "| Stage id | Label | Probability | Closed | Required to enter (set in UI) |",
                "|---|---|---|---|---|",
            ]
            for stage in pipeline.stages:
                required = ", ".join(f"`{p}`" for p in stage.required_properties) or "none"
                out.append(
                    f"| `{stage.id}` | {_cell(stage.label)} | {stage.probability:.0%} | "
                    f"{'yes' if stage.closed else 'no'} | {required} |"
                )
            out += [""]

    if spec.association_labels:
        out += ["## Association labels", "", "| From | To | Name | Label | Inverse label |"]
        out += ["|---|---|---|---|---|"]
        for label in spec.association_labels:
            out.append(
                f"| {label.from_object} | {label.to_object} | `{label.name}` | "
                f"{_cell(label.label)} | {_cell(label.inverse_label or label.label)} |"
            )
        out += [""]

    if spec.lifecycle:
        out += ["## Lifecycle stages", "", "| Stage | Definition | Entry criteria | Owner |"]
        out += ["|---|---|---|---|"]
        for life in spec.lifecycle:
            out.append(
                f"| {_cell(life.stage)} | {_cell(life.definition)} | {_cell(life.entry)} | "
                f"{_cell(life.owner)} |"
            )
        out += [""]

    if spec.manual:
        out += ["## Manual steps (HubSpot UI only)", "", "| Area | Step | Why |", "|---|---|---|"]
        for step in spec.manual:
            out.append(f"| {_cell(step.area)} | {_cell(step.step)} | {_cell(step.why)} |")
        out += [""]

    return "\n".join(out)
