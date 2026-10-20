"""Command-line entry point.

Typer keeps each command a plain typed function, so commands stay testable through
``CliRunner`` without spawning a process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pipelineos import __version__
from pipelineos.data.clean import clean, write_clean
from pipelineos.data.generate import GenConfig, generate, write_dataset, write_summary
from pipelineos.guard import GuardError, WriteMode, authorize_writes
from pipelineos.hubspot import FakeHubSpot, HttpHubSpot, HubSpotClient, HubSpotError
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import build_plan
from pipelineos.portal.render import render_apply, render_checklist, render_plan
from pipelineos.settings import get_settings
from pipelineos.spec import PortalSpec, SpecError, load_spec
from pipelineos.spec.docs import render_field_dictionary

DEFAULT_SPEC = Path("portal/pipelineos.yaml")
DEFAULT_DICTIONARY = Path("docs/field-dictionary.md")

SpecOption = Annotated[Path, typer.Option("--spec", help="Portal spec YAML.")]
FakeOption = Annotated[
    bool, typer.Option("--fake", help="Run against an empty in-memory HubSpot (no token needed).")
]
YesOption = Annotated[bool, typer.Option("--yes", help="Actually write. Without it: preview.")]

# `plan` exit codes, like `terraform plan -detailed-exitcode`, so CI can gate on drift.
EXIT_NO_CHANGES = 0
EXIT_ERROR = 1
EXIT_CHANGES_PENDING = 2

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


def _load(spec_path: Path) -> PortalSpec:
    try:
        return load_spec(spec_path)
    except SpecError as exc:
        for problem in exc.problems:
            typer.echo(f"error: {problem}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc


def _client(fake: bool) -> tuple[HubSpotClient, int | None]:
    """The client to use and the portal id writes are allowed against."""
    if fake:
        hs = FakeHubSpot()
        return hs, hs.portal  # in-memory, so writing to it is always safe
    settings = get_settings()
    try:
        return HttpHubSpot.from_settings(settings), settings.allowed_portal
    except HubSpotError as exc:
        typer.echo(f"error: {exc.message} (or use --fake)", err=True)
        raise typer.Exit(EXIT_ERROR) from exc


@app.command()
def plan(
    spec: SpecOption = DEFAULT_SPEC,
    fake: FakeOption = False,
    verbose: Annotated[bool, typer.Option(help="Also list unchanged items.")] = False,
) -> None:
    """Show what apply would change. Exit code 2 when changes are pending."""
    portal = _load(spec)
    client, _ = _client(fake)
    try:
        the_plan = build_plan(portal, read_live(client, portal))
    except HubSpotError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    typer.echo(f"PipelineOS plan for {portal.client}\n")
    typer.echo(render_plan(the_plan, verbose=verbose))
    typer.echo("\n" + render_checklist(portal))
    if the_plan.changes:
        raise typer.Exit(EXIT_CHANGES_PENDING)


@app.command()
def apply(
    spec: SpecOption = DEFAULT_SPEC, fake: FakeOption = False, yes: YesOption = False
) -> None:
    """Create and update what the plan lists. Previews unless --yes; never deletes."""
    portal = _load(spec)
    client, allowed = _client(fake)
    try:
        live = read_live(client, portal)
        the_plan = build_plan(portal, live)
        typer.echo(f"PipelineOS apply for {portal.client}\n")
        typer.echo(render_plan(the_plan))
        if not the_plan.changes:
            typer.echo("\nNothing to apply.")
            return
        mode = authorize_writes(client, yes=yes, allowed_portal=allowed)
        if mode is WriteMode.PREVIEW:
            typer.echo(
                f"\nPreview only. Re-run with --yes to apply {len(the_plan.changes)} changes."
            )
            return
        result = apply_plan(client, the_plan, live)
        typer.echo("\n" + render_apply(result, len(the_plan.changes)))
        if not result.ok:
            raise typer.Exit(EXIT_ERROR)
        after = build_plan(portal, read_live(client, portal))
        typer.echo(f"Re-plan after apply: {len(after.changes)} changes pending.")
        typer.echo("\n" + render_checklist(portal))
        if after.changes:
            raise typer.Exit(EXIT_ERROR)
    except GuardError as exc:
        typer.echo(f"\nerror: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    except HubSpotError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc


@app.command(name="generate")
def generate_cmd(
    n: Annotated[int, typer.Option(help="Distinct real people to generate.")] = 3000,
    dirty: Annotated[float, typer.Option(help="Share of records that get a defect.")] = 0.2,
    seed: Annotated[int, typer.Option(help="Random seed; same seed, same files.")] = 7,
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("data/raw"),
    report: Annotated[Path | None, typer.Option(help="Also write the summary JSON here.")] = None,
) -> None:
    """Generate a messy CRM import (contacts + companies) with separate ground truth."""
    data, summary = generate(GenConfig(n=n, dirty_fraction=dirty, seed=seed))
    write_dataset(data, out)
    write_summary(summary, out / "summary.json")
    if report is not None:
        write_summary(summary, report)
    typer.echo(
        f"wrote {summary['contact_rows']} contact rows ({summary['duplicate_rows']} duplicates) "
        f"and {summary['company_rows']} company rows to {out}"
    )


@app.command(name="clean")
def clean_cmd(
    raw: Annotated[Path, typer.Argument(help="Directory with contacts.csv and companies.csv.")] = (
        Path("data/raw")
    ),
    out: Annotated[Path, typer.Option(help="Where golden records go.")] = Path("data/clean"),
    report: Annotated[Path | None, typer.Option(help="Also write the report JSON here.")] = None,
) -> None:
    """Normalize, dedupe and merge a raw import into golden records."""
    result = clean(raw)
    write_clean(result, out)
    write_summary(result.report, out / "report.json")
    if report is not None:
        write_summary(result.report, report)
    r = result.report
    before = r["quality_before"]["score"]  # type: ignore[index]
    after = r["quality_after"]["score"]  # type: ignore[index]
    contacts = r["contacts"]
    typer.echo(
        f"contacts: {contacts['rows_in']} rows -> {contacts['golden_out']} golden "  # type: ignore[index]
        f"({contacts['merged_away']} merged, {contacts['rejected']} rejected); "  # type: ignore[index]
        f"quality {before} -> {after}"
    )
    if "accuracy_contacts" in r:
        pw = r["accuracy_contacts"]["pairwise"]  # type: ignore[index]
        typer.echo(f"vs ground truth: precision {pw['precision']}, recall {pw['recall']}")
