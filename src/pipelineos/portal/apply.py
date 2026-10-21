"""Execute a plan's create/update actions against a HubSpot client.

Actions run in the planner's dependency order. A custom object created earlier in the same
apply gets its ``objectTypeId`` from the create response, and later actions on that object use
it. The applier stops at the first failure: every action is idempotent at the plan level, so the
fix is to correct the cause and run ``plan``/``apply`` again, which picks up where this stopped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipelineos.hubspot.client import STANDARD_OBJECT_IDS, HubSpotClient, HubSpotError
from pipelineos.portal.live import LiveState
from pipelineos.portal.plan import Action, Op, Plan

# HubSpot needs type and fieldType on every property update; name is the path key and
# hasUniqueValue can never change, so neither belongs in the PATCH body.
_NOT_IN_PROPERTY_PATCH = ("name", "hasUniqueValue")


@dataclass
class ApplyResult:
    applied: list[Action] = field(default_factory=list)
    failed: Action | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.failed is None


class _Applier:
    def __init__(self, client: HubSpotClient, live: LiveState) -> None:
        self.client = client
        self.api_types = {name: obj.api_type for name, obj in live.objects.items()}

    def api_type(self, object_ref: str) -> str:
        if object_ref in STANDARD_OBJECT_IDS:
            return object_ref
        return self.api_types[object_ref]

    def type_id(self, object_ref: str) -> str:
        return STANDARD_OBJECT_IDS.get(object_ref) or self.api_types[object_ref]

    def run(self, action: Action) -> None:
        c, ref, p = self.client, action.object_ref, action.payload
        create = action.op is Op.CREATE
        match action.kind:
            case "group" if create:
                c.create_property_group(self.api_type(ref), p)
            case "group":
                patch = {k: p[k] for k in ("label", "displayOrder")}
                c.update_property_group(self.api_type(ref), action.name, patch)
            case "property" if create:
                c.create_property(self.api_type(ref), p)
            case "property":
                patch = {k: v for k, v in p.items() if k not in _NOT_IN_PROPERTY_PATCH}
                c.update_property(self.api_type(ref), action.name, patch)
            case "schema" if create:
                created = c.create_schema(p)
                self.api_types[ref] = str(created["objectTypeId"])
            case "schema":
                c.update_schema(self.api_type(ref), p)
            case "validation":
                c.put_validation_rule(self.type_id(ref), action.parent, action.name, p)
            case "pipeline" if create:
                c.create_pipeline(ref, p)
            case "pipeline":
                c.update_pipeline(ref, action.live_id, p)
            case "stage" if create:
                c.create_stage(ref, action.live_id, p)
            case "stage":
                c.update_stage(ref, action.live_id, action.name, p)
            case "label":
                c.create_association_label(self.api_type(ref), self.api_type(action.parent), p)


def apply_plan(client: HubSpotClient, plan: Plan, live: LiveState) -> ApplyResult:
    applier = _Applier(client, live)
    result = ApplyResult()
    for action in plan.changes:
        try:
            applier.run(action)
        except (HubSpotError, KeyError) as exc:
            result.failed = action
            result.error = str(exc)
            return result
        result.applied.append(action)
    return result
