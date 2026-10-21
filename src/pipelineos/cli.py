"""Command-line entry point.

Typer keeps each command a plain typed function, so commands stay testable through
``CliRunner`` without spawning a process.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from pipelineos import __version__
from pipelineos.data.clean import clean, write_clean
from pipelineos.data.generate import GenConfig, generate, write_dataset, write_summary
from pipelineos.data.load import load, preview
from pipelineos.guard import GuardError, WriteMode, authorize_writes
from pipelineos.hubspot import FakeHubSpot, HttpHubSpot, HubSpotClient, HubSpotError
from pipelineos.portal.apply import apply_plan
from pipelineos.portal.live import read_live
from pipelineos.portal.plan import build_plan
from pipelineos.portal.render import render_apply, render_checklist, render_plan
from pipelineos.settings import get_settings
from pipelineos.spec import PortalSpec, SpecError, load_spec
from pipelineos.spec.docs import render_field_dictionary
from pipelineos.usage.events import DEFAULT_AS_OF, generate_events, read_events, write_events
from pipelineos.usage.score import aggregate, load_pql_config, score

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


def _client(fake: bool, configure: PortalSpec | None = None) -> tuple[HubSpotClient, int | None]:
    """The client to use and the portal id writes are allowed against.

    With ``configure``, a fake portal is first set up from the spec, standing in for a real
    portal that has already been through ``pipelineos apply``.
    """
    if fake:
        hs = FakeHubSpot()
        if configure is not None:
            live = read_live(hs, configure)
            apply_plan(hs, build_plan(configure, live), live)
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


@app.command(name="load")
def load_cmd(
    clean_dir: Annotated[Path, typer.Argument(help="Output of `pipelineos clean`.")] = (
        Path("data/clean")
    ),
    spec: SpecOption = DEFAULT_SPEC,
    fake: FakeOption = False,
    yes: YesOption = False,
    report: Annotated[Path | None, typer.Option(help="Write the load report JSON here.")] = None,
) -> None:
    """Upsert golden companies and contacts, then associate them. Previews unless --yes."""
    portal = _load(spec)
    client, allowed = _client(fake, configure=portal)
    planned = preview(clean_dir)
    typer.echo(
        f"Load {planned['companies']} companies and {planned['contacts']} contacts in "
        f"{planned['upsert_calls']} upsert calls, then up to {planned['associations']} "
        "contact-company associations."
    )
    try:
        mode = authorize_writes(client, yes=yes, allowed_portal=allowed)
        if mode is WriteMode.PREVIEW:
            typer.echo("Preview only. Re-run with --yes to write.")
            return
        result = load(client, clean_dir)
    except (GuardError, HubSpotError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    summary = result.summary()
    if report is not None:
        write_summary(summary, report)
    for name, part in (("companies", result.companies), ("contacts", result.contacts)):
        typer.echo(
            f"{name}: {part.created} created, {part.updated} updated, {len(part.errors)} failed "
            f"({part.batches} batches)"
        )
        for error in part.errors[:5]:
            typer.echo(f"  ! {error.key}: {error.message}")
    typer.echo(
        f"associations: {result.associations} created, {result.skipped_associations} skipped"
    )
    if result.companies.errors or result.contacts.errors:
        raise typer.Exit(EXIT_ERROR)


usage_app = typer.Typer(
    help="Product usage: generate events and score workspaces.", no_args_is_help=True
)
app.add_typer(usage_app, name="usage")

PqlOption = Annotated[Path, typer.Option("--pql", help="PQL scoring rules YAML.")]
AsOfOption = Annotated[str, typer.Option("--as-of", help="Scoring date (YYYY-MM-DD).")]
DEFAULT_PQL = Path("portal/pql.yaml")
DEFAULT_AS_OF_TEXT = DEFAULT_AS_OF.isoformat()


@usage_app.command(name="generate")
def usage_generate(
    clean_dir: Annotated[Path, typer.Argument(help="Output of `pipelineos clean`.")] = (
        Path("data/clean")
    ),
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("data/usage"),
    seed: Annotated[int, typer.Option(help="Random seed.")] = 7,
    as_of: AsOfOption = DEFAULT_AS_OF_TEXT,
) -> None:
    """Generate product events for workspaces built on the cleaned CRM data."""
    contacts = read_csv_rows(clean_dir / "contacts.csv")
    companies = read_csv_rows(clean_dir / "companies.csv")
    events, archetypes = generate_events(
        contacts, companies, seed=seed, as_of=date.fromisoformat(as_of)
    )
    write_events(events, out / "events.csv")
    with (out / "truth_archetypes.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("workspace_id", "archetype"))
        writer.writerows(sorted(archetypes.items()))
    typer.echo(f"wrote {len(events)} events for {len(archetypes)} workspaces to {out}")


@usage_app.command(name="score")
def usage_score(
    events_path: Annotated[Path, typer.Argument(help="events.csv")] = Path("data/usage/events.csv"),
    clean_dir: Annotated[Path, typer.Option(help="Cleaned CRM data, for company size.")] = (
        Path("data/clean")
    ),
    pql: PqlOption = DEFAULT_PQL,
    as_of: AsOfOption = DEFAULT_AS_OF_TEXT,
    top: Annotated[int, typer.Option(help="How many top workspaces to list.")] = 10,
    report: Annotated[Path | None, typer.Option(help="Write a JSON summary here.")] = None,
) -> None:
    """Score every workspace and list the top product-qualified ones with their reasons."""
    config = load_pql_config(pql)
    day = date.fromisoformat(as_of)
    facts = aggregate(read_events(events_path), day, config.window_days)
    employees = company_sizes(clean_dir / "companies.csv")
    results = {
        w: score(f, config, day, employees.get(f.company_key or "")) for w, f in facts.items()
    }
    ranked = sorted(results, key=lambda w: (-results[w].score, w))
    pqls = [w for w in ranked if results[w].is_pql]
    typer.echo(
        f"{len(pqls)} of {len(results)} workspaces are product-qualified "
        f"(threshold {config.threshold})\n"
    )
    for w in ranked[:top]:
        r = results[w]
        typer.echo(f"  {r.score:>3}  {facts[w].name}  [{facts[w].plan}, {facts[w].seats} seats]")
        typer.echo(f"       {r.reasons_text}")
    summary: dict[str, object] = {
        "workspaces": len(results),
        "pql": len(pqls),
        "threshold": config.threshold,
        "as_of": as_of,
    }
    truth = events_path.parent / "truth_archetypes.csv"
    if truth.exists():
        by_type: dict[str, list[int]] = defaultdict(list)
        for truth_row in read_csv_rows(truth):
            found = results.get(truth_row["workspace_id"])
            if found is not None:
                by_type[truth_row["archetype"]].append(found.score)
        breakdown = {
            name: {
                "workspaces": len(scores),
                "pql_rate": round(sum(s >= config.threshold for s in scores) / len(scores), 3),
                "mean_score": round(sum(scores) / len(scores), 1),
            }
            for name, scores in sorted(by_type.items())
        }
        summary["by_archetype"] = breakdown
        typer.echo("\nPQL rate by workspace type (generator ground truth):")
        for name, stats in breakdown.items():
            typer.echo(
                f"  {name:<16} {stats['pql_rate']:>5.0%} of {stats['workspaces']:>3}, "
                f"mean score {stats['mean_score']}"
            )
    if report is not None:
        write_summary(summary, report)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def company_sizes(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {
        row["company_key"]: int(row["numberofemployees"])
        for row in read_csv_rows(path)
        if row.get("numberofemployees", "").isdigit()
    }
