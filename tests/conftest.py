from __future__ import annotations

from pathlib import Path

import pytest

from pipelineos.spec import PortalSpec, load_spec

SPEC_PATH = Path(__file__).resolve().parents[1] / "portal" / "pipelineos.yaml"


@pytest.fixture
def spec() -> PortalSpec:
    return load_spec(SPEC_PATH)
