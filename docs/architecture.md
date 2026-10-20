# Architecture and decisions

The short version of each design decision, the alternatives considered, and why this one won.

## One client interface, two implementations

Every HubSpot call goes through the `HubSpotClient` Protocol (`hubspot/client.py`). `HttpHubSpot`
talks to the API with httpx; `FakeHubSpot` keeps state in memory. Commands take the Protocol, so
the whole pipeline runs in tests and CI without a token, and `--fake` gives a full demo on a laptop.

The fake is only worth having if it is strict. It enforces the HubSpot rules this project relies
on: unique-key upserts, the 100-input batch cap, no repeated key in a batch, unknown properties,
picklist values, email format, validation rules on writes, and fields that can never change.
Modelling those rules shaped two decisions early: the load never sends the import's free-text
`industry` (HubSpot's is a fixed picklist), and companies are upserted by `company_key`, not
`domain`.

## No official SDK

`hubspot-api-client` 12.0.0 (May 2025) predates HubSpot's date-versioned paths
(`/crm/objects/2026-09/...`, live since March 2026). The surface this project needs is about 20
calls, small enough to own. Owning it gives one place for the API version (`hubspot/paths.py`),
explicit control of batching, 207 partial failures, rate limiting and retries, and no stale
generated dependency.

## Plan and apply, never delete

`portal/plan.py` is a pure function from (spec, live snapshot) to actions, in dependency order:
groups before properties, schema creation before anything on the custom object, new properties
before a schema update that references them. It never plans a delete:

- Anything live that the spec does not mention is **unmanaged**: shown, left alone.
- Changes HubSpot will not make in place (a property's type or uniqueness, a custom object's
  labels or associations) are **manual**, with the reason. Doing them automatically would mean
  deleting records.

Pipelines are matched by label, because HubSpot assigns pipeline ids. Stages are matched by id,
because HubSpot lets us set stage ids, which keeps the diff stable.

## `company_key` instead of `domain`

HubSpot's `domain` is not a unique property, so companies cannot be upserted by it. PipelineOS
adds `company_key`, the normalized registrable domain (`https://www.acme.io/pricing` becomes
`acme.io`), as a unique property with DOMAIN and lowercase validation rules, and upserts on that.

## PQL is a property, scored per workspace

HubSpot only moves lifecycle stage forward through the API, so a custom "PQL" stage could not be
undone without clearing it first. PQL lives on properties instead: `pql_score`, `is_pql` and
`pql_reasons` on the `Workspace` object, rolled up to its company. The workspace is scored, not
the person, because a team adopting the product is what buys Enterprise. Every rule in
`portal/pql.yaml` carries one sentence that ends up in `pql_reasons`, so the number a rep sees
always comes with why.

## Sync does not touch lifecycle stage or lead status

Those belong to reps and to the HubSpot workflow that reacts to `is_pql`. A nightly job that
writes them would overwrite what a rep set by hand. Sync writes product facts only.

## Sync sends only what changed

Each upsert counts against the rate limit and writes an audit entry even when nothing changed.
The state file keeps a hash of the properties last sent and the record id for each key, so a run
with no new usage makes zero write calls. The record id is also what lets a new association
point at a record that did not change in this run.

## Dedupe: exact keys first, fuzzy only inside blocks

Comparing every pair is O(n^2): 77 million comparisons at 12,653 rows. PipelineOS merges on
exact keys first (normalized email with Gmail dots and `+tags` removed; phone plus last name),
then compares names only inside (company domain, last-name prefix) blocks. Measured at 12,653
rows: 4,192 comparisons and 0.15 s. Two records with different phone numbers are never merged on
name alone, which keeps namesakes at the same company apart.

## The model reads text; rules decide routing

The classifier extracts three facts from a demo-form answer: use case, enterprise asks, team
size. `ai/route.py` decides the queue with rules a RevOps lead can read and change, and company
size from the CRM wins over a size typed into the form. The model is constrained to a JSON
schema at temperature 0. Any failure falls back to keyword rules, and the output says which
one ran. A model-reported team size is kept only if that number is written in the answer, so an
invented size cannot move a lead to a different team.

## Write safety

Every command that writes previews by default. Writing needs `--yes` and a token whose portal id
matches `PIPELINEOS_ALLOWED_PORTAL` (`guard.py`).
