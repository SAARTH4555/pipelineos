from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipelineos import __version__
from pipelineos.cli import app

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
