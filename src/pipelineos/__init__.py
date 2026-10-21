"""PipelineOS: a HubSpot RevOps build expressed as code.

The package is organised by job, not by layer: ``portal`` turns a YAML spec into HubSpot
configuration, ``data`` cleans and loads records, ``usage`` syncs product usage and scores
product-qualified workspaces, and ``ai`` routes demo requests. Every HubSpot call goes through
one client interface so the whole thing runs against an in-memory fake in tests and CI.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
