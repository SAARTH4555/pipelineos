"""The portal spec: a typed model of everything PipelineOS manages in HubSpot."""

from __future__ import annotations

from pipelineos.spec.load import SpecError, load_spec
from pipelineos.spec.models import (
    CustomObjectSpec,
    PipelineSpec,
    PortalSpec,
    PropertySpec,
    StageSpec,
)

__all__ = [
    "CustomObjectSpec",
    "PipelineSpec",
    "PortalSpec",
    "PropertySpec",
    "SpecError",
    "StageSpec",
    "load_spec",
]
