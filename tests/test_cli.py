from __future__ import annotations

from typer.testing import CliRunner

from pipelineos import __version__
from pipelineos.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "HubSpot RevOps build as code" in result.output
