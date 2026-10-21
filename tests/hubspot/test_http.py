from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from pipelineos.hubspot.client import AssociationInput, HubSpotError, UpsertInput
from pipelineos.hubspot.http import HttpHubSpot, parse_batch_response
from pipelineos.settings import Settings
from tests.support import FakeClock, RecordingSleeper

Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler, *, max_retries: int = 5) -> tuple[HttpHubSpot, RecordingSleeper]:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test")
    client = HttpHubSpot(
        "tok-123",
        api_version="2026-09",
        http=http,
        clock=clock,
        sleeper=sleeper,
        rng=lambda: 1.0,
        max_retries=max_retries,
    )
    return client, sleeper


def test_requests_carry_bearer_token_and_dated_path() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"results": [{"name": "email"}]})

    client, _ = make(handler)
    assert client.list_properties("contacts") == [{"name": "email"}]
    assert seen[0].url.path == "/crm/properties/2026-09/contacts"
    assert seen[0].headers["Authorization"] == "Bearer tok-123"


def test_portal_id() -> None:
    client, _ = make(lambda req: httpx.Response(200, json={"portalId": 42}))
    assert client.portal_id() == 42


def test_429_then_success_backs_off_with_jitter_ceiling() -> None:
    responses = iter([httpx.Response(429), httpx.Response(429), httpx.Response(200, json={})])
    client, sleeper = make(lambda req: next(responses))
    assert client.create_property("contacts", {"name": "x"}) == {}
    assert sleeper.sleeps == [0.5, 1.0]


def test_retry_after_header_wins_over_backoff() -> None:
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    client, sleeper = make(lambda req: next(responses))
    client.create_property("contacts", {"name": "x"})
    assert sleeper.sleeps == [7.0]


def test_423_locked_waits_at_least_two_seconds() -> None:
    responses = iter([httpx.Response(423), httpx.Response(200, json={})])
    client, sleeper = make(lambda req: next(responses))
    client.create_property("contacts", {"name": "x"})
    assert sleeper.sleeps == [2.0]


def test_retries_exhausted_raises_last_error() -> None:
    client, sleeper = make(
        lambda req: httpx.Response(503, json={"category": "UNAVAILABLE", "message": "down"}),
        max_retries=2,
    )
    with pytest.raises(HubSpotError) as info:
        client.list_schemas()
    assert info.value.status == 503
    assert info.value.category == "UNAVAILABLE"
    assert len(sleeper.sleeps) == 2


def test_transport_error_is_retried_then_raised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    client, sleeper = make(handler, max_retries=1)
    with pytest.raises(HubSpotError) as info:
        client.list_schemas()
    assert info.value.category == "TRANSPORT"
    assert sleeper.sleeps == [0.5]


def test_client_errors_are_not_retried() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(409, json={"category": "CONFLICT", "message": "exists"})

    client, sleeper = make(handler)
    with pytest.raises(HubSpotError) as info:
        client.create_property_group("contacts", {"name": "g", "label": "G"})
    assert (info.value.status, info.value.category) == (409, "CONFLICT")
    assert len(calls) == 1
    assert sleeper.sleeps == []


def test_non_json_error_body_still_raises() -> None:
    client, _ = make(lambda req: httpx.Response(400, text="<html>bad</html>"))
    with pytest.raises(HubSpotError) as info:
        client.list_pipelines("deals")
    assert info.value.category == "HTTP_ERROR"
    assert "bad" in info.value.message


def test_every_endpoint_hits_its_versioned_path() -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(204)

    client, _ = make(handler)
    client.list_property_groups("contacts")
    client.create_property_group("contacts", {})
    client.update_property_group("contacts", "g", {})
    client.update_property("contacts", "p", {})
    client.create_schema({})
    client.update_schema("2-1", {})
    client.create_pipeline("deals", {})
    client.update_pipeline("deals", "9", {})
    client.create_stage("deals", "9", {})
    client.update_stage("deals", "9", "s1", {})
    client.list_association_labels("contacts", "companies")
    client.create_association_label("contacts", "companies", {})
    assert seen == [
        ("GET", "/crm/properties/2026-09/contacts/groups"),
        ("POST", "/crm/properties/2026-09/contacts/groups"),
        ("PATCH", "/crm/properties/2026-09/contacts/groups/g"),
        ("PATCH", "/crm/properties/2026-09/contacts/p"),
        ("POST", "/crm-object-schemas/2026-09/schemas"),
        ("PATCH", "/crm-object-schemas/2026-09/schemas/2-1"),
        ("POST", "/crm/pipelines/2026-09/deals"),
        ("PATCH", "/crm/pipelines/2026-09/deals/9"),
        ("POST", "/crm/pipelines/2026-09/deals/9/stages"),
        ("PATCH", "/crm/pipelines/2026-09/deals/9/stages/s1"),
        ("GET", "/crm/associations/2026-09/contacts/companies/labels"),
        ("POST", "/crm/associations/2026-09/contacts/companies/labels"),
    ]


def test_batch_upsert_sends_trace_ids_and_parses_207() -> None:
    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(
            207,
            json={
                "status": "COMPLETE",
                "results": [{"id": "11", "new": True, "properties": {"company_key": "acme.io"}}],
                "numErrors": 1,
                "errors": [
                    {
                        "category": "VALIDATION_ERROR",
                        "message": "bad option",
                        "context": {"objectWriteTraceId": ["beta.dev"]},
                    }
                ],
            },
        )

    client, _ = make(handler)
    result = client.batch_upsert(
        "companies",
        "company_key",
        [UpsertInput("acme.io", {"name": "Acme"}), UpsertInput("beta.dev", {"name": "Beta"})],
    )
    sent = bodies[0]["inputs"]
    assert isinstance(sent, list)
    assert sent[0] == {
        "id": "acme.io",
        "idProperty": "company_key",
        "properties": {"name": "Acme"},
        "objectWriteTraceId": "acme.io",
    }
    assert (result.created, result.updated) == (1, 0)
    assert result.results[0].record_id == "11"
    assert result.errors[0].key == "beta.dev"


def test_batch_upsert_rejects_bad_batches_before_sending() -> None:
    client, _ = make(lambda req: pytest.fail("should not send"))
    with pytest.raises(ValueError, match="duplicate"):
        client.batch_upsert("contacts", "email", [UpsertInput("a@x.io", {})] * 2)
    with pytest.raises(ValueError, match="cap"):
        client.batch_upsert("contacts", "email", [UpsertInput(f"{i}@x.io", {}) for i in range(101)])
    assert client.batch_upsert("contacts", "email", []).results == []


def test_batch_associate_body_and_errors() -> None:
    bodies: list[dict[str, object]] = []
    replies = iter(
        [
            httpx.Response(200, json={"results": [{}, {}]}),
            httpx.Response(
                207, json={"numErrors": 1, "errors": [{"category": "X", "message": "m"}]}
            ),
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return next(replies)

    client, _ = make(handler)
    pairs = [AssociationInput("1", "2"), AssociationInput("3", "4")]
    assert client.batch_associate("contacts", "companies", 279, "HUBSPOT_DEFINED", pairs) == 2
    assert bodies[0]["inputs"] == [
        {
            "from": {"id": "1"},
            "to": {"id": "2"},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 279}],
        },
        {
            "from": {"id": "3"},
            "to": {"id": "4"},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 279}],
        },
    ]
    with pytest.raises(HubSpotError):
        client.batch_associate("contacts", "companies", 279, "HUBSPOT_DEFINED", pairs)
    assert client.batch_associate("contacts", "companies", 279, "HUBSPOT_DEFINED", []) == 0
    with pytest.raises(ValueError):
        client.batch_associate(
            "contacts", "companies", 279, "HUBSPOT_DEFINED", [AssociationInput("1", "2")] * 2001
        )


def test_parse_batch_response_handles_missing_fields() -> None:
    result = parse_batch_response({"results": [{"id": 5}], "errors": [{}]}, "email")
    assert result.results[0].key == ""
    assert result.results[0].created is False
    assert result.errors[0].key is None


def test_from_settings_requires_token() -> None:
    with pytest.raises(HubSpotError, match="TOKEN"):
        HttpHubSpot.from_settings(Settings(hubspot_token=None))
    client = HttpHubSpot.from_settings(Settings(hubspot_token=SecretStr("t")))
    assert isinstance(client, HttpHubSpot)
