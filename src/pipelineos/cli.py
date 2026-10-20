"""Command-line entry point.

Typer keeps each command a plain typed function, so commands stay testable through
``CliRunner`` without spawning a process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pipelineos import __version__
from pipelineos.spec import SpecError, load_spec
from pipelineos.spec.docs import render_field_dictionary

DEFAULT_SPEC = Path("portal/pipelineos.yaml")
DEFAULT_DICTIONARY = Path("docs/field-dictionary.md")

SpecOption = Annotated[Path, typer.Option("--spec", help="Portal spec YAML.")]

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


@app.command()
def validate(spec: SpecOption = DEFAULT_SPEC) -> None:
    """Check a portal spec without touching HubSpot."""
    try:
        portal = load_spec(spec)
    except SpecError as exc:
        for problem in exc.problems:
            typer.echo(f"error: {problem}", err=True)
        raise typer.Exit(1) from exc
    properties = sum(len(o.properties) for o in portal.objects.values())
    properties += sum(len(c.properties) for c in portal.custom_objects)
    stages = sum(len(p.stages) for ps in portal.pipelines.values() for p in ps)
    typer.echo(
        f"ok: {spec} ({properties} properties, {len(portal.custom_objects)} custom objects, "
        f"{stages} pipeline stages, {len(portal.association_labels)} association labels)"
    )


@app.command()
def dictionary(
    spec: SpecOption = DEFAULT_SPEC,
    out: Annotated[Path, typer.Option(help="Where to write the field dictionary.")] = (
        DEFAULT_DICTIONARY
    ),
    check: Annotated[bool, typer.Option(help="Fail if the committed file is stale.")] = False,
) -> None:
    """Generate the field dictionary from the spec."""
    try:
        portal = load_spec(spec)
    except SpecError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    rendered = render_field_dictionary(portal, spec.as_posix())
    if check:
        current = out.read_text() if out.exists() else ""
        if current != rendered:
            typer.echo(f"{out} is stale: run `pipelineos dictionary`", err=True)
            raise typer.Exit(1)
        typer.echo(f"{out} is up to date")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)
    typer.echo(f"wrote {out}")
