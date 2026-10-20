"""Every HubSpot URL path, built from one date-versioned API release.

HubSpot moved to date-based versions in March 2026 (``/crm/objects/2026-09/contacts`` instead of
``/crm/v3/objects/contacts``) and the version segment does not always sit where ``v3`` used to.
Keeping all paths here means a version bump, or a path that turns out different in a live
portal, is a one-file change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Paths:
    version: str

    def account_details(self) -> str:
        return f"/account-info/{self.version}/details"

    def property_groups(self, object_type: str) -> str:
        return f"/crm/properties/{self.version}/{object_type}/groups"

    def property_group(self, object_type: str, name: str) -> str:
        return f"{self.property_groups(object_type)}/{name}"

    def properties(self, object_type: str) -> str:
        return f"/crm/properties/{self.version}/{object_type}"

    def property(self, object_type: str, name: str) -> str:
        return f"{self.properties(object_type)}/{name}"

    def schemas(self) -> str:
        return f"/crm-object-schemas/{self.version}/schemas"

    def schema(self, object_type_id: str) -> str:
        return f"{self.schemas()}/{object_type_id}"

    def pipelines(self, object_type: str) -> str:
        return f"/crm/pipelines/{self.version}/{object_type}"

    def pipeline(self, object_type: str, pipeline_id: str) -> str:
        return f"{self.pipelines(object_type)}/{pipeline_id}"

    def stages(self, object_type: str, pipeline_id: str) -> str:
        return f"{self.pipeline(object_type, pipeline_id)}/stages"

    def stage(self, object_type: str, pipeline_id: str, stage_id: str) -> str:
        return f"{self.stages(object_type, pipeline_id)}/{stage_id}"

    def association_labels(self, from_type: str, to_type: str) -> str:
        return f"/crm/associations/{self.version}/{from_type}/{to_type}/labels"

    def batch_upsert(self, object_type: str) -> str:
        return f"/crm/objects/{self.version}/{object_type}/batch/upsert"

    def batch_associate(self, from_type: str, to_type: str) -> str:
        return f"/crm/associations/{self.version}/{from_type}/{to_type}/batch/create"
