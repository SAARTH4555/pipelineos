"""``HubSpotClient`` over HTTP with httpx.

Calls the REST API directly rather than through ``hubspot-api-client``: the SDK's last release
(12.0.0, May 2025) predates HubSpot's date-versioned paths, and the surface this project needs is
small enough to own. Every request takes a token from the bucket first; 429, 423 and 5xx
responses are retried with backoff, everything else non-2xx raises ``HubSpotError``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from typing import Any

import httpx

from pipelineos.hubspot.client import (
    JSON,
    MAX_ASSOCIATION_BATCH,
    AssociationInput,
    BatchError,
    BatchResult,
    HubSpotError,
    RecordResult,
    UpsertInput,
    check_upsert_batch,
)
from pipelineos.hubspot.paths import Paths
from pipelineos.hubspot.ratelimit import (
    LOCKED_FLOOR_S,
    Clock,
    Rng,
    Sleeper,
    TokenBucket,
    backoff_delay,
    parse_retry_after,
)
from pipelineos.settings import Settings

RETRYABLE = frozenset({423, 429, 502, 503, 504})
# Free/Starter apps get 100 requests per 10 s; stay under it so bursts never hit a 429.
BUCKET_CAPACITY = 90
BUCKET_INTERVAL_S = 10.0


class HttpHubSpot:
    def __init__(
        self,
        token: str,
        *,
        api_version: str = "2026-09",
        base_url: str = "https://api.hubapi.com",
        http: httpx.Client | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
        rng: Rng = random.random,
        max_retries: int = 5,
    ) -> None:
        self._paths = Paths(api_version)
        self._http = http or httpx.Client(base_url=base_url, timeout=30.0)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._sleep = sleeper
        self._rng = rng
        self._max_retries = max_retries
        self._bucket = TokenBucket(BUCKET_CAPACITY, BUCKET_INTERVAL_S, clock=clock, sleeper=sleeper)

    @classmethod
    def from_settings(cls, settings: Settings) -> HttpHubSpot:
        if settings.hubspot_token is None:
            raise HubSpotError(0, "CONFIG", "PIPELINEOS_HUBSPOT_TOKEN is not set")
        return cls(
            settings.hubspot_token.get_secret_value(),
            api_version=settings.hubspot_api_version,
            base_url=settings.hubspot_base_url,
        )

    # -- transport ---------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: JSON | None = None) -> JSON:
        for attempt in range(self._max_retries + 1):
            self._bucket.acquire()
            last = attempt == self._max_retries
            try:
                resp = self._http.request(method, path, json=body, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise HubSpotError(0, "TRANSPORT", str(exc)) from exc
                self._sleep(backoff_delay(attempt, rng=self._rng))
                continue
            if resp.status_code in RETRYABLE and not last:
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise _error_from(resp)
            if not resp.content:
                return {}
            data: JSON = resp.json()
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        delay = parse_retry_after(resp.headers.get("Retry-After"))
        if delay is None:
            delay = backoff_delay(attempt, rng=self._rng)
        if resp.status_code == 423:
            delay = max(delay, LOCKED_FLOOR_S)
        return delay

    def _results(self, method: str, path: str) -> list[JSON]:
        results: list[JSON] = self._request(method, path).get("results", [])
        return results

    # -- account -----------------------------------------------------------------------------

    def portal_id(self) -> int:
        return int(self._request("GET", self._paths.account_details())["portalId"])

    # -- property groups and properties ------------------------------------------------------

    def list_property_groups(self, object_type: str) -> list[JSON]:
        return self._results("GET", self._paths.property_groups(object_type))

    def create_property_group(self, object_type: str, group: JSON) -> JSON:
        return self._request("POST", self._paths.property_groups(object_type), group)

    def update_property_group(self, object_type: str, name: str, patch: JSON) -> JSON:
        return self._request("PATCH", self._paths.property_group(object_type, name), patch)

    def list_properties(self, object_type: str) -> list[JSON]:
        return self._results("GET", self._paths.properties(object_type))

    def create_property(self, object_type: str, prop: JSON) -> JSON:
        return self._request("POST", self._paths.properties(object_type), prop)

    def update_property(self, object_type: str, name: str, patch: JSON) -> JSON:
        return self._request("PATCH", self._paths.property(object_type, name), patch)

    # -- custom object schemas ---------------------------------------------------------------

    def list_schemas(self) -> list[JSON]:
        return self._results("GET", self._paths.schemas())

    def create_schema(self, schema: JSON) -> JSON:
        return self._request("POST", self._paths.schemas(), schema)

    def update_schema(self, object_type_id: str, patch: JSON) -> JSON:
        return self._request("PATCH", self._paths.schema(object_type_id), patch)

    # -- pipelines ---------------------------------------------------------------------------

    def list_pipelines(self, object_type: str) -> list[JSON]:
        return self._results("GET", self._paths.pipelines(object_type))

    def create_pipeline(self, object_type: str, pipeline: JSON) -> JSON:
        return self._request("POST", self._paths.pipelines(object_type), pipeline)

    def update_pipeline(self, object_type: str, pipeline_id: str, patch: JSON) -> JSON:
        return self._request("PATCH", self._paths.pipeline(object_type, pipeline_id), patch)

    def create_stage(self, object_type: str, pipeline_id: str, stage: JSON) -> JSON:
        return self._request("POST", self._paths.stages(object_type, pipeline_id), stage)

    def update_stage(self, object_type: str, pipeline_id: str, stage_id: str, patch: JSON) -> JSON:
        path = self._paths.stage(object_type, pipeline_id, stage_id)
        return self._request("PATCH", path, patch)

    # -- association labels ------------------------------------------------------------------

    def list_association_labels(self, from_type: str, to_type: str) -> list[JSON]:
        return self._results("GET", self._paths.association_labels(from_type, to_type))

    def create_association_label(self, from_type: str, to_type: str, label: JSON) -> JSON:
        return self._request("POST", self._paths.association_labels(from_type, to_type), label)

    # -- property validation rules -----------------------------------------------------------

    def list_validation_rules(self, object_type_id: str) -> list[JSON]:
        rows = self._results("GET", self._paths.validation_rules(object_type_id))
        return flatten_validation_rules(rows)

    def put_validation_rule(
        self, object_type_id: str, property_name: str, rule_type: str, body: JSON
    ) -> None:
        path = self._paths.validation_rule(object_type_id, property_name, rule_type)
        self._request("PUT", path, body)

    # -- records -----------------------------------------------------------------------------

    def batch_upsert(
        self, object_type: str, id_property: str, inputs: Sequence[UpsertInput]
    ) -> BatchResult:
        check_upsert_batch(inputs)
        if not inputs:
            return BatchResult()
        body: JSON = {
            "inputs": [
                {
                    "id": item.key,
                    "idProperty": id_property,
                    "properties": dict(item.properties),
                    # Lets a 207 error be traced back to the row that caused it.
                    "objectWriteTraceId": item.key,
                }
                for item in inputs
            ]
        }
        data = self._request("POST", self._paths.batch_upsert(object_type), body)
        return parse_batch_response(data, id_property)

    def batch_associate(
        self,
        from_type: str,
        to_type: str,
        type_id: int,
        category: str,
        pairs: Sequence[AssociationInput],
    ) -> int:
        if len(pairs) > MAX_ASSOCIATION_BATCH:
            raise ValueError(f"{len(pairs)} associations exceeds the {MAX_ASSOCIATION_BATCH} cap")
        if not pairs:
            return 0
        types = [{"associationCategory": category, "associationTypeId": type_id}]
        body: JSON = {
            "inputs": [
                {"from": {"id": p.from_id}, "to": {"id": p.to_id}, "types": types} for p in pairs
            ]
        }
        data = self._request("POST", self._paths.batch_associate(from_type, to_type), body)
        if data.get("numErrors"):
            first = data.get("errors", [{}])[0]
            raise HubSpotError(207, first.get("category", "BATCH_ERROR"), first.get("message", ""))
        return len(data.get("results", []))


def parse_batch_response(data: JSON, id_property: str) -> BatchResult:
    """Turn a 200/207 batch-upsert body into per-row results and per-row errors."""
    result = BatchResult()
    for row in data.get("results", []):
        props: dict[str, Any] = row.get("properties") or {}
        result.results.append(
            RecordResult(
                key=str(props.get(id_property, "")),
                record_id=str(row["id"]),
                created=bool(row.get("new", False)),
            )
        )
    for err in data.get("errors", []):
        trace = (err.get("context") or {}).get("objectWriteTraceId") or [None]
        result.errors.append(
            BatchError(
                key=trace[0],
                category=str(err.get("category", "UNKNOWN")),
                message=str(err.get("message", "")),
            )
        )
    return result


def flatten_validation_rules(rows: list[JSON]) -> list[JSON]:
    """Accept per-property groups (``propertyValidationRules: [...]``) or already-flat rows.

    The list endpoint's exact response shape was not confirmed against a live portal, so both
    plausible shapes are normalised here, in one place.
    """
    flat: list[JSON] = []
    for row in rows:
        nested = row.get("propertyValidationRules")
        if nested is None:
            flat.append(row)
            continue
        for rule in nested:
            flat.append({"propertyName": row.get("propertyName"), **rule})
    return flat


def _error_from(resp: httpx.Response) -> HubSpotError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    category = body.get("category") or body.get("errorType") or "HTTP_ERROR"
    message = body.get("message") or resp.text[:200]
    return HubSpotError(resp.status_code, str(category), str(message))
