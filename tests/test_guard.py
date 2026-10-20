from __future__ import annotations

import pytest

from pipelineos.guard import GuardError, WriteMode, authorize_writes
from pipelineos.hubspot.fake import FakeHubSpot
from pipelineos.settings import Settings


def test_preview_without_yes_never_asks_for_the_portal() -> None:
    hs = FakeHubSpot()
    assert authorize_writes(hs, yes=False, allowed_portal=None) is WriteMode.PREVIEW
    assert hs.calls == []


def test_yes_without_allowed_portal_is_refused() -> None:
    with pytest.raises(GuardError, match="PIPELINEOS_ALLOWED_PORTAL"):
        authorize_writes(FakeHubSpot(), yes=True, allowed_portal=None)


def test_yes_with_wrong_portal_is_refused() -> None:
    with pytest.raises(GuardError, match="portal 111"):
        authorize_writes(FakeHubSpot(portal=111), yes=True, allowed_portal=222)


def test_yes_with_matching_portal_writes() -> None:
    assert authorize_writes(FakeHubSpot(portal=7), yes=True, allowed_portal=7) is WriteMode.WRITE


def test_settings_read_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINEOS_ALLOWED_PORTAL", "42")
    monkeypatch.setenv("PIPELINEOS_HUBSPOT_TOKEN", "secret")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.allowed_portal == 42
    assert settings.hubspot_token is not None
    assert settings.hubspot_token.get_secret_value() == "secret"
    assert "secret" not in repr(settings)
    assert settings.hubspot_api_version == "2026-09"
