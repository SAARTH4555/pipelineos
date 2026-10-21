"""Rewrite the README's results section from results/*.json, or check that it is current.

Usage: uv run python scripts/render_readme.py [--check]

Nothing between the RESULTS markers is typed by hand. CI runs --check, so a README number can
only change by re-running the pipeline (scripts/reproduce.sh) and committing its output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
README = ROOT / "README.md"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"


def load(name: str) -> Any:
    return json.loads((RESULTS / f"{name}.json").read_text())


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def portal_section() -> list[str]:
    a = load("apply_fake")
    plan = a["plan"]
    return [
        "### Portal configuration as code",
        "",
        "Applied to an empty in-memory portal (`pipelineos apply --fake --yes`).",
        "",
        "| | |",
        "|---|---|",
        f"| Changes planned on an empty portal | {plan['create']} creates, {plan['update']} "
        f"updates, {plan['unmanaged']} unmanaged (HubSpot's default pipeline, left alone) |",
        f"| Applied | {a['applied']} of {plan['create'] + plan['update']} |",
        f"| Changes pending when planning again | {a['changes_after_replan']} |",
        f"| UI-only work printed as a checklist | {a['manual_checklist_items']} items, including "
        f"{a['stage_required_property_rules']} stage-required property rules |",
        "",
    ]


def cleanup_section() -> list[str]:
    g, c = load("generated"), load("cleanup")
    acc, comp = c["accuracy_contacts"], c["accuracy_companies"]
    before, after = c["quality_before"], c["quality_after"]
    cfg = g["config"]
    lines = [
        "### Data cleanup",
        "",
        f"Generated import: {g['people']:,} real people as {g['contact_rows']:,} contact rows "
        f"({g['duplicate_rows']:,} duplicates, {acc['junk']['rows']} junk rows) and "
        f"{g['company_rows']} company rows for {g['companies']} companies "
        f"(`dirty_fraction={cfg['dirty_fraction']}`, `seed={cfg['seed']}`).",
        "",
        "| Quality score (no ground truth needed) | Before | After |",
        "|---|---|---|",
    ]
    for part in ("validity", "uniqueness", "standardization", "completeness"):
        lines.append(f"| {part.capitalize()} | {pct(before[part])} | {pct(after[part])} |")
    lines += [
        f"| **Score (0-100)** | **{before['score']}** | **{after['score']}** |",
        "",
        "| Against ground truth | |",
        "|---|---|",
        f"| Rows in, golden records out | {c['contacts']['rows_in']:,} in, "
        f"{c['contacts']['golden_out']:,} out ({c['contacts']['merged_away']} merged, "
        f"{c['contacts']['rejected']} rejected) |",
        f"| Duplicate pairs: precision / recall | {acc['pairwise']['precision']} / "
        f"{acc['pairwise']['recall']} |",
        f"| Duplicate rate before, after | {pct(acc['duplicate_rate_before'])}, "
        f"{pct(acc['duplicate_rate_after'])} |",
        f"| Junk rows rejected | {acc['junk']['rejected']} of {acc['junk']['rows']}, "
        f"{acc['junk']['valid_rows_wrongly_rejected']} valid rows wrongly rejected |",
        f"| Company domain assigned correctly | {pct(comp['key_accuracy'])} of company rows |",
        "",
        "| Duplicate kind | Rows | Caught |",
        "|---|---|---|",
    ]
    for kind, rows in acc["duplicates_by_kind"].items():
        lines.append(f"| {kind.replace('_', ' ')} | {rows} | {pct(acc['recall_by_kind'][kind])} |")
    lines.append("")
    return lines


def bench_section() -> list[str]:
    runs = load("bench")["runs"]
    lines = [
        "### Dedupe cost",
        "",
        f"`scripts/bench_dedupe.py` on {runs[0]['machine']}; normalize + match + merge, best of 3.",
        "",
        "| People | Rows | Time | Peak memory | Comparisons | All pairs |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        lines.append(
            f"| {r['people']:,} | {r['rows']:,} | {r['seconds_best']:.3f} s | "
            f"{r['peak_memory_mb']} MB | {r['comparisons']:,} | {r['naive_comparisons']:,} |"
        )
    lines.append("")
    return lines


def usage_section() -> list[str]:
    pql, sync, load_ = load("pql"), load("sync_fake"), load("load_fake")
    lines = [
        "### Product usage and PQL",
        "",
        f"{pql['pql']} of {pql['workspaces']} workspaces product-qualified "
        f"(threshold {pql['threshold']}, as of {pql['as_of']}). Each workspace type below is a "
        "behaviour the generator was built to produce, so this shows the rules separate those "
        "behaviours. It is not a claim about real-world accuracy.",
        "",
        "| Workspace type | Workspaces | Flagged as PQL | Mean score |",
        "|---|---|---|---|",
    ]
    for name, row in pql["by_archetype"].items():
        lines.append(
            f"| {name.replace('_', ' ')} | {row['workspaces']} | {pct(row['pql_rate'])} | "
            f"{row['mean_score']} |"
        )
    lines += [
        "",
        "### Loading and syncing (in-memory portal)",
        "",
        "Each command ran against its own fresh in-memory portal, so `sync` creates the "
        "companies and contacts that `load` created in its run.",
        "",
        "| Step | Companies | Contacts | Workspaces | Associations |",
        "|---|---|---|---|---|",
        f"| `load` golden records | {load_['companies']['created']:,} created | "
        f"{load_['contacts']['created']:,} created | | {load_['associations']:,} |",
        f"| `sync` product usage | {sync['companies']['created']:,} created | "
        f"{sync['contacts']['created']:,} created | {sync['workspaces']['created']:,} created | "
        f"{sync['associations']['created']:,} |",
        f"| `sync` again, no new usage | | | | {sync['resync_writes']} records sent in total |",
        "",
    ]
    return lines


def fallback_note(results: dict[str, Any]) -> str:
    fallbacks = sum(r["sources"].get("keyword_fallback", 0) for r in results.values())
    total = sum(r["examples"] for r in results.values())
    if fallbacks == 0:
        return f"All {total} answers came from the model; none fell back to keyword rules."
    return f"{fallbacks} of {total} answers fell back to keyword rules."


def classifier_section() -> list[str]:
    kw, ol = load("classifier_keyword"), load("classifier_ollama")
    model = ol["holdout"]["provider"].removeprefix("ollama:")

    def row(label: str, metric: str) -> str:
        return (
            f"| {label} | {pct(kw['dev']['accuracy'][metric])} | "
            f"{pct(kw['holdout']['accuracy'][metric])} | {pct(ol['dev']['accuracy'][metric])} | "
            f"**{pct(ol['holdout']['accuracy'][metric])}** |"
        )

    return [
        "### Demo-request classifier",
        "",
        f"Hand-labelled answers: a dev set ({kw['dev']['examples']}) that was in view while the "
        f"keyword rules were written, and a holdout ({kw['holdout']['examples']}) written "
        "afterwards and never used to tune anything. Quote the holdout.",
        "",
        f"| | Keyword, dev | Keyword, holdout | {model}, dev | {model}, holdout |",
        "|---|---|---|---|---|",
        row("Use case", "use_case"),
        row("Enterprise signals", "enterprise_signals"),
        row("Team size", "team_size"),
        row("**Routed to the right queue**", "routing"),
        "",
        f"{model} ran locally through Ollama on {ol['holdout']['machine']}, "
        f"{ol['holdout']['seconds_per_answer']} s per answer. {fallback_note(ol)}",
        "",
    ]


def render() -> str:
    lines = [START, ""]
    for section in (
        portal_section,
        cleanup_section,
        bench_section,
        usage_section,
        classifier_section,
    ):
        lines += section()
    lines.append(END)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = README.read_text()
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    updated = head + render() + tail
    if args.check:
        if updated != text:
            print("README results are stale: run scripts/render_readme.py", file=sys.stderr)
            sys.exit(1)
        print("README results are current")
        return
    README.write_text(updated)
    print("README results updated")


if __name__ == "__main__":
    main()
