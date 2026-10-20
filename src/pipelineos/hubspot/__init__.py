"""HubSpot access: one Protocol, an httpx implementation and an in-memory fake."""

from __future__ import annotations

from pipelineos.hubspot.client import (
    JSON,
    AssociationInput,
    BatchError,
    BatchResult,
    HubSpotClient,
    HubSpotError,
    RecordResult,
    UpsertInput,
)
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.hubspot.http import HttpHubSpot

__all__ = [
    "JSON",
    "AssociationInput",
    "BatchError",
    "BatchResult",
    "FakeHubSpot",
    "HttpHubSpot",
    "HubSpotClient",
    "HubSpotError",
    "RecordResult",
    "UpsertInput",
]
