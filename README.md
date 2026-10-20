# PipelineOS

A HubSpot RevOps build, as code. One YAML file sets up the portal, a messy CRM import gets
cleaned and loaded, product usage is scored so sales knows which free workspaces to call, and
demo requests are routed by a local LLM. Every step previews before it writes, never deletes,
and backs its numbers with a committed script.

The client is **Quarry**, a fictional Series A dev-tools company: engineers sign up free and
invite their team, the Team plan is self-serve, and Enterprise (SSO, audit log, on-prem runners)
is sold by a three-person sales team.

## Why it matters

A RevOps engagement at a startup usually comes down to four jobs:

1. Set the CRM up the way the company actually sells: objects, properties, pipelines, rules.
2. Clean the data that is already in it.
3. Get product signals in front of sales.
4. Route inbound leads to the right person, fast, with a reason they can trust.

Done by hand in the HubSpot UI, none of that can be reviewed in a pull request, repeated in a
new portal, or tested. PipelineOS does each job as code that a reviewer can read and CI can run.

## What it does

| Command | Job |
|---|---|
| `pipelineos plan` / `apply` | Diffs [`portal/pipelineos.yaml`](portal/pipelineos.yaml) against the portal, then creates or updates property groups, properties, validation rules, the `Workspace` custom object, deal pipelines and association labels. Previews by default, never deletes, prints the UI-only steps as a checklist. |
| `generate` / `clean` | Builds a messy import with known duplicates, then normalizes, dedupes and merges it into golden records with an audit trail. |
| `load` | Upserts golden companies (by `company_key`) and contacts (by email) in batches, then associates them. |
| `usage generate` / `usage score` / `sync` | Scores product workspaces with rules in [`portal/pql.yaml`](portal/pql.yaml), each point with a plain-English reason, and syncs workspaces, company roll-ups and members to HubSpot. Records that did not change are never re-sent. |
| `classify` / `route-demos` | Classifies demo-form answers with a local model (keyword rules as fallback) and writes segment, queue and reason onto the contact. |
| `dictionary` | Generates [`docs/field-dictionary.md`](docs/field-dictionary.md) from the spec. |

## Architecture

```
 portal/pipelineos.yaml -> spec/ (validate) -> portal/plan.py (diff) -> portal/apply.py --+
                                                     ^                                   |
                                         portal/live.py (read)                           |
 data/raw/*.csv -> data/clean.py (normalize, dedupe, score) -> data/load.py -------------+
 events.csv -> usage/score.py (facts, PQL) -> usage/sync.py (+ state file) --------------+
 demo answers -> ai/classify.py (Ollama | keywords) -> ai/route.py ----------------------+
                                                                                         |
                    guard.py: preview unless --yes AND the portal id is the allowed one  |
                                                                                         v
                    hubspot/client.py   one Protocol for every HubSpot call
                      |- http.py        httpx, date-versioned paths, token bucket, backoff, 207s
                      '- fake.py        in-memory HubSpot that rejects what HubSpot rejects
```

The fake matters. It refuses what a real portal refuses: companies upserted by `domain` (not a
unique property), batches over 100, a repeated key in one batch, unknown properties, values
outside a picklist, invalid emails, writes that break a validation rule, and changes to fields
HubSpot never lets you change. A bug that would fail in a real portal fails in the tests first.
Design decisions and their reasons are in [`docs/architecture.md`](docs/architecture.md).

## Quickstart

Python 3.12 and [uv](https://docs.astral.sh/uv/). No HubSpot account is needed for this part:
`--fake` runs everything against the in-memory portal.

```bash
uv sync --group dev
uv run pipelineos plan --fake            # what an empty portal needs
uv run pipelineos apply --fake --yes     # apply it, then plan again: 0 changes
uv run pipelineos generate               # messy import with ground truth
uv run pipelineos clean                  # golden records + report
uv run pipelineos load --fake --yes
uv run pipelineos usage generate
uv run pipelineos usage score
uv run pipelineos sync --fake --yes
uv run pipelineos route-demos --fake     # add --ollama to classify with a local model
```

To run it against a real HubSpot developer test account, follow
[`docs/runbook.md`](docs/runbook.md).

## Demo

`pipelineos plan --fake`, abridged:

```
PipelineOS plan for Quarry (fictional)

  + group      contacts.pipelineos_lead               Lead management
  + property   contacts.product_user_id               string/text
  + validation companies.company_key DOMAIN
  + validation companies.pql_score MAX_NUMBER         100
  + validation workspace.workspace_id REGEX           ^ws_[a-z0-9]{6,}$
  + pipeline   deals: New Business                    7 stages
  + label      workspace -> contacts: Admin           workspace_admin
  ? pipeline   deals: Sales Pipeline                  not in spec (left alone)

Plan: 42 to create, 0 to update, 0 need manual action, 1 unmanaged (left alone), 0 unchanged.
```

`pipelineos route-demos --fake` (keyword rules):

```
maya.chen@ledgerline.io      ae_enterprise       flaky_tests      flaky tests; 1,400 people (CRM); asked for enterprise requirements
omar@tinyforge.dev           self_serve_nurture  slow_ci          slow ci; 8 people (CRM); no enterprise asks
priya.n@carelink.health      ae_enterprise       compliance       compliance; 220 people (CRM); asked for enterprise requirements
lee@gmail.com                self_serve_nurture  other            No testing problem described; size unknown
sofia.r@shipyard.co          sdr_mid_market      test_visibility  test visibility; 90 people (CRM); no enterprise asks
dan@brightcart.com           self_serve_nurture  other            No testing problem described; 30 people (form)
```

Read that as: every lead gets a queue and a reason a rep can check. The last row is the keyword
rules' weak spot: "brittle e2e tests" is not in their vocabulary, so it reads as no problem at
all. With `--ollama`, the local model labels it `flaky_tests`. The classifier results below
measure exactly that gap.

## Results

Every number here comes from `results/*.json`, written by the commands in
[`scripts/reproduce.sh`](scripts/reproduce.sh). CI fails if this section and those files
disagree.

<!-- RESULTS:START -->

### Portal configuration as code

Applied to an empty in-memory portal (`pipelineos apply --fake --yes`).

| | |
|---|---|
| Changes planned on an empty portal | 42 creates, 0 updates, 1 unmanaged (HubSpot's default pipeline, left alone) |
| Applied | 42 of 42 |
| Changes pending when planning again | 0 |
| UI-only work printed as a checklist | 6 items, including 19 stage-required property rules |

### Data cleanup

Generated import: 3,000 real people as 3,827 contact rows (766 duplicates, 61 junk rows) and 480 company rows for 375 companies (`dirty_fraction=0.2`, `seed=7`).

| Quality score (no ground truth needed) | Before | After |
|---|---|---|
| Validity | 98.4% | 100.0% |
| Uniqueness | 76.5% | 100.0% |
| Standardization | 41.5% | 100.0% |
| Completeness | 97.1% | 99.0% |
| **Score (0-100)** | **78.4** | **99.8** |

| Against ground truth | |
|---|---|
| Rows in, golden records out | 3,827 in, 2,995 out (771 merged, 61 rejected) |
| Duplicate pairs: precision / recall | 0.9912 / 1.0 |
| Duplicate rate before, after | 20.3%, 0.0% |
| Junk rows rejected | 61 of 61, 0 valid rows wrongly rejected |
| Company domain assigned correctly | 99.2% of company rows |

| Duplicate kind | Rows | Caught |
|---|---|---|
| alt work email | 257 | 100.0% |
| email variant | 220 | 100.0% |
| exact | 203 | 100.0% |
| gmail alias | 12 | 100.0% |
| personal email | 74 | 100.0% |

### Dedupe cost

`scripts/bench_dedupe.py` on arm64 / Python 3.12.13; normalize + match + merge, best of 3.

| People | Rows | Time | Peak memory | Comparisons | All pairs |
|---|---|---|---|---|---|
| 1,000 | 1,276 | 0.014 s | 1.3 MB | 327 | 788,140 |
| 3,000 | 3,827 | 0.043 s | 4.0 MB | 1,124 | 7,089,495 |
| 10,000 | 12,653 | 0.144 s | 13.3 MB | 4,192 | 77,407,903 |

### Product usage and PQL

83 of 240 workspaces product-qualified (threshold 60, as of 2026-10-01). Each workspace type below is a behaviour the generator was built to produce, so this shows the rules separate those behaviours. It is not a claim about real-world accuracy.

| Workspace type | Workspaces | Flagged as PQL | Mean score |
|---|---|---|---|
| dormant | 41 | 0.0% | 0.0 |
| enterprise | 14 | 100.0% | 92.1 |
| evaluating team | 50 | 94.0% | 76.8 |
| hobbyist | 113 | 0.0% | 20.8 |
| paying team | 22 | 100.0% | 91.8 |

### Loading and syncing (in-memory portal)

Each command ran against its own fresh in-memory portal, so `sync` creates the companies and contacts that `load` created in its run.

| Step | Companies | Contacts | Workspaces | Associations |
|---|---|---|---|---|
| `load` golden records | 374 created | 2,995 created | | 2,966 |
| `sync` product usage | 211 created | 945 created | 240 created | 1,156 |
| `sync` again, no new usage | | | | 0 records sent in total |

### Demo-request classifier

Hand-labelled answers: a dev set (48) that was in view while the keyword rules were written, and a holdout (24) written afterwards and never used to tune anything. Quote the holdout.

| | Keyword, dev | Keyword, holdout | llama3.1:8b, dev | llama3.1:8b, holdout |
|---|---|---|---|---|
| Use case | 97.9% | 41.7% | 89.6% | **95.8%** |
| Enterprise signals | 100.0% | 79.2% | 100.0% | **91.7%** |
| Team size | 100.0% | 95.8% | 95.8% | **100.0%** |
| **Routed to the right queue** | 100.0% | 79.2% | 100.0% | **95.8%** |

llama3.1:8b ran locally through Ollama on arm64 / Python 3.12.13, 1.728 s per answer. All 72 answers came from the model; none fell back to keyword rules.

<!-- RESULTS:END -->

## What is real and what is not

- **The data is synthetic.** It is generated from a seed, with ground truth in separate files
  that only the evaluation reads. That is what makes precision and recall measurable at all.
- **The PQL table checks the rules against behaviours the generator was built to produce.** It
  shows the rules separate those behaviours, not that they would on Quarry's real users.
- **The HubSpot client has not yet been run against a live portal.** The fake is built from
  HubSpot's 2026-09 API docs. [`docs/runbook.md`](docs/runbook.md) lists the details to confirm
  on the first live run.
- **The Ollama numbers are from a real local run.** The holdout is small (24 answers), so read
  them as an order of magnitude.

## Testing

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                                  # strict, src + tests + scripts
uv run pipelineos validate && uv run pipelineos dictionary --check
uv run python scripts/render_readme.py --check
uv run pytest --cov                          # fails under 85% branch coverage
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same gates in three jobs
(quality, test, build). It needs no secrets: no step talks to HubSpot or to a model.

## Layout

```
portal/            pipelineos.yaml (the portal), pql.yaml (scoring rules)
src/pipelineos/
  spec/            typed spec, validation, field-dictionary renderer
  portal/          live snapshot, planner, applier, plan rendering
  hubspot/         client Protocol, httpx client, fake, rate limiting, paths
  data/            generator, normalizers, dedupe, quality scoring, cleanup, load
  usage/           events, facts and PQL scoring, sync state, sync
  ai/              classifier (Ollama + keywords), routing rules
  guard.py         the write gate
evals/             hand-labelled demo-form answers (dev + holdout)
examples/          sample demo requests
scripts/           reproduce.sh, bench_dedupe.py, eval_classifier.py, render_readme.py
results/           committed outputs behind every README number
docs/              runbook, architecture, generated field dictionary
```

## What I would build next

- **Workflows as code**, once HubSpot's v4 workflows API leaves beta. Until then, an `export`
  that snapshots the UI-built workflows to JSON so they can at least be reviewed.
- **Split PQL from expansion.** Paying workspaces currently score as PQL too. Routing them to the
  Expansion pipeline instead of an SDR is a small rule on `plan`.
- **Enrichment before load**: firmographics and tech stack from Clay, so `account_segment` does
  not depend on a self-reported employee count.
- **Scheduled sync**: a nightly GitHub Actions run with the state file kept in object storage.
