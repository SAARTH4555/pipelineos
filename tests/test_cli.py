from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipelineos import __version__
from pipelineos.cli import app
from pipelineos.hubspot import FakeHubSpot, HubSpotError
from pipelineos.settings import get_settings

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "portal" / "pipelineos.yaml"


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "HubSpot RevOps build as code" in result.output


def test_validate_ok() -> None:
    result = runner.invoke(app, ["validate", "--spec", str(SPEC)])
    assert result.exit_code == 0
    assert "1 custom objects" in result.stdout


def test_validate_reports_problems(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\n")
    result = runner.invoke(app, ["validate", "--spec", str(bad)])
    assert result.exit_code == 1
    assert "error: version" in result.output


def test_dictionary_write_then_check(tmp_path: Path) -> None:
    out = tmp_path / "dict.md"
    assert runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out)]).exit_code == 0
    ok = runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out), "--check"])
    assert ok.exit_code == 0
    out.write_text(out.read_text() + "drift\n")
    stale = runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out), "--check"])
    assert stale.exit_code == 1
    assert "stale" in stale.output


def test_dictionary_bad_spec(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dictionary", "--spec", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_committed_field_dictionary_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["dictionary", "--check"], catch_exceptions=False)
    assert result.exit_code == 0, result.output


def test_plan_fake_exits_2_when_changes_pending() -> None:
    result = runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 2
    assert "Plan: 42 to create" in result.stdout
    assert "Manual checklist" in result.stdout


def test_apply_fake_previews_without_yes() -> None:
    result = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 0
    assert "Preview only. Re-run with --yes to apply 42 changes." in result.stdout


def test_apply_fake_yes_converges() -> None:
    result = runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 0, result.output
    assert "Applied 42 of 42 changes." in result.stdout
    assert "Re-plan after apply: 0 changes pending." in result.stdout


def test_converged_portal_has_nothing_to_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = FakeHubSpot()
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: shared)
    assert runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)]).exit_code == 0
    again = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert "Nothing to apply." in again.stdout
    assert runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)]).exit_code == 0


def _real_client(monkeypatch: pytest.MonkeyPatch, portal: int, allowed: str | None) -> None:
    monkeypatch.setenv("PIPELINEOS_HUBSPOT_TOKEN", "tok")
    if allowed is None:
        monkeypatch.delenv("PIPELINEOS_ALLOWED_PORTAL", raising=False)
    else:
        monkeypatch.setenv("PIPELINEOS_ALLOWED_PORTAL", allowed)
    get_settings.cache_clear()
    fake = FakeHubSpot(portal=portal)
    monkeypatch.setattr("pipelineos.cli.HttpHubSpot.from_settings", lambda settings: fake)


def test_apply_refuses_wrong_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=111, allowed="222")
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "token belongs to portal 111" in result.output
    get_settings.cache_clear()


def test_apply_refuses_without_allowed_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=111, allowed=None)
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "PIPELINEOS_ALLOWED_PORTAL" in result.output
    get_settings.cache_clear()


def test_apply_writes_to_allowed_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=333, allowed="333")
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 0, result.output
    assert "Re-plan after apply: 0 changes pending." in result.stdout
    get_settings.cache_clear()


def test_real_mode_without_token_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINEOS_HUBSPOT_TOKEN", raising=False)
    monkeypatch.chdir(ROOT / "tests")  # no .env here
    get_settings.cache_clear()
    result = runner.invoke(app, ["plan", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "PIPELINEOS_HUBSPOT_TOKEN is not set (or use --fake)" in result.output
    get_settings.cache_clear()


def test_hubspot_errors_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = FakeHubSpot()
    broken.fail_next = [HubSpotError(503, "UNAVAILABLE", "down")] * 2
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: broken)
    assert runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)]).exit_code == 1
    result = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 1 and "down" in result.output


def test_apply_reports_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = FakeHubSpot()

    def fail_schema(schema: dict[str, object]) -> dict[str, object]:
        raise HubSpotError(400, "VALIDATION_ERROR", "schema rejected")

    flaky.create_schema = fail_schema  # type: ignore[method-assign]
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: flaky)
    result = runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "then failed on schema workspace" in result.stdout


def test_generate_writes_files_and_report(tmp_path: Path) -> None:
    out, report = tmp_path / "raw", tmp_path / "results" / "generated.json"
    result = runner.invoke(
        app, ["generate", "--n", "200", "--seed", "4", "--out", str(out), "--report", str(report)]
    )
    assert result.exit_code == 0, result.output
    assert "contact rows" in result.stdout
    for name in ("contacts.csv", "companies.csv", "truth_contacts.csv", "summary.json"):
        assert (out / name).exists()
    assert report.read_text() == (out / "summary.json").read_text()


def test_clean_command(tmp_path: Path) -> None:
    raw, out, report = tmp_path / "raw", tmp_path / "clean", tmp_path / "cleanup.json"
    assert runner.invoke(app, ["generate", "--n", "300", "--out", str(raw)]).exit_code == 0
    result = runner.invoke(app, ["clean", str(raw), "--out", str(out), "--report", str(report)])
    assert result.exit_code == 0, result.output
    assert "golden" in result.stdout and "vs ground truth: precision" in result.stdout
    assert report.exists() and (out / "report.json").exists()
