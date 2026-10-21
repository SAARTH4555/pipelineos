"""The one gate in front of every HubSpot write.

Commands that write (``apply``, ``load``, ``sync``) preview by default. Writing needs both an
explicit ``--yes`` and a portal id that matches ``PIPELINEOS_ALLOWED_PORTAL``, so a token that
belongs to a real company's portal cannot be written to by accident, even with ``--yes``.
"""

from __future__ import annotations

from enum import Enum

from pipelineos.hubspot.client import HubSpotClient


class WriteMode(Enum):
    PREVIEW = "preview"
    WRITE = "write"


class GuardError(Exception):
    """Raised when a write was requested but is not allowed."""


def authorize_writes(client: HubSpotClient, *, yes: bool, allowed_portal: int | None) -> WriteMode:
    """Return ``WRITE`` only when ``yes`` is set and the client's portal is the allowed one."""
    if not yes:
        return WriteMode.PREVIEW
    if allowed_portal is None:
        raise GuardError(
            "refusing to write: set PIPELINEOS_ALLOWED_PORTAL to the test portal's id first"
        )
    actual = client.portal_id()
    if actual != allowed_portal:
        raise GuardError(
            f"refusing to write: token belongs to portal {actual}, "
            f"but PIPELINEOS_ALLOWED_PORTAL is {allowed_portal}"
        )
    return WriteMode.WRITE
