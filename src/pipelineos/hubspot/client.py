"""The one interface every HubSpot call goes through.

``HubSpotClient`` is a Protocol with two implementations: ``HttpHubSpot`` (httpx, real API) and
``FakeHubSpot`` (in-memory, enforces the HubSpot rules this project depends on). Config resources
(properties, schemas, pipelines, labels) travel as HubSpot's own JSON shapes, because the planner
diffs against exactly what the API returns. Record writes use small typed inputs and results,
because those are where partial failures need to be traced back to a specific row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

JSON = dict[str, Any]

# HubSpot's documented per-request input caps.
MAX_UPSERT_BATCH = 100
MAX_ASSOCIATION_BATCH = 2000

# Built-in object type ids, used when an endpoint wants an id rather than a name.
STANDARD_OBJECT_IDS: Mapping[str, str] = {"contacts": "0-1", "companies": "0-2", "deals": "0-3"}


class HubSpotError(Exception):
    """A non-retryable HubSpot API failure (or a retryable one that exhausted its retries)."""

    def __init__(self, status: int, category: str, message: str) -> None:
        super().__init__(f"{status} {category}: {message}")
        self.status = status
        self.category = category
        self.message = message


@dataclass(frozen=True)
class UpsertInput:
    """One record to upsert. ``key`` is the value of the batch's unique ``id_property``."""

    key: str
    properties: Mapping[str, str]


@dataclass(frozen=True)
class RecordResult:
    key: str
    record_id: str
    created: bool


@dataclass(frozen=True)
class BatchError:
    key: str | None
    category: str
    message: str


@dataclass
class BatchResult:
    results: list[RecordResult] = field(default_factory=list)
    errors: list[BatchError] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for r in self.results if r.created)

    @property
    def updated(self) -> int:
        return sum(1 for r in self.results if not r.created)


@dataclass(frozen=True)
class AssociationInput:
    from_id: str
    to_id: str


def check_upsert_batch(inputs: Sequence[UpsertInput]) -> None:
    """Reject a batch HubSpot would reject, before spending a request on it."""
    if len(inputs) > MAX_UPSERT_BATCH:
        raise ValueError(f"batch of {len(inputs)} exceeds HubSpot's {MAX_UPSERT_BATCH}-input cap")
    seen: set[str] = set()
    for item in inputs:
        if item.key in seen:
            raise ValueError(f"duplicate key {item.key!r} in one batch (HubSpot rejects these)")
        seen.add(item.key)


class HubSpotClient(Protocol):
    def portal_id(self) -> int: ...

    # property groups and properties
    def list_property_groups(self, object_type: str) -> list[JSON]: ...
    def create_property_group(self, object_type: str, group: JSON) -> JSON: ...
    def update_property_group(self, object_type: str, name: str, patch: JSON) -> JSON: ...
    def list_properties(self, object_type: str) -> list[JSON]: ...
    def create_property(self, object_type: str, prop: JSON) -> JSON: ...
    def update_property(self, object_type: str, name: str, patch: JSON) -> JSON: ...

    # custom object schemas
    def list_schemas(self) -> list[JSON]: ...
    def create_schema(self, schema: JSON) -> JSON: ...
    def update_schema(self, object_type_id: str, patch: JSON) -> JSON: ...

    # pipelines
    def list_pipelines(self, object_type: str) -> list[JSON]: ...
    def create_pipeline(self, object_type: str, pipeline: JSON) -> JSON: ...
    def update_pipeline(self, object_type: str, pipeline_id: str, patch: JSON) -> JSON: ...
    def create_stage(self, object_type: str, pipeline_id: str, stage: JSON) -> JSON: ...
    def update_stage(
        self, object_type: str, pipeline_id: str, stage_id: str, patch: JSON
    ) -> JSON: ...

    # association labels
    def list_association_labels(self, from_type: str, to_type: str) -> list[JSON]: ...
    def create_association_label(self, from_type: str, to_type: str, label: JSON) -> JSON: ...

    # property validation rules (enforced on API writes since the 2026-09 release)
    def list_validation_rules(self, object_type_id: str) -> list[JSON]:
        """Flat rows: ``{propertyName, ruleType, ruleArguments, shouldApplyNormalization}``."""
        ...

    def put_validation_rule(
        self, object_type_id: str, property_name: str, rule_type: str, body: JSON
    ) -> None: ...

    # records
    def batch_upsert(
        self, object_type: str, id_property: str, inputs: Sequence[UpsertInput]
    ) -> BatchResult: ...
    def batch_associate(
        self,
        from_type: str,
        to_type: str,
        type_id: int,
        category: str,
        pairs: Sequence[AssociationInput],
    ) -> int: ...
