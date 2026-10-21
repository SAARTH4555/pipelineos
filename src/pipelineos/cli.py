"""Command-line entry point.

Typer keeps each command a plain typed function, so commands stay testable through
``CliRunner`` without spawning a process.
"""

from __future__ import annotations

import typer

from pipelineos import __version__

app = typer.Typer(
    help="HubSpot RevOps build as code.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """PipelineOS: plan and apply HubSpot config, clean data, sync product usage."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
