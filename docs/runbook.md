# Runbook: PipelineOS against a real HubSpot portal

This takes a new HubSpot developer test account from empty to configured, loaded and syncing.
It assumes the quickstart in the README already works with `--fake`.

## 1. Get a test portal and a key

1. Create a free HubSpot developer account, then a **developer test account** from it
   (Test accounts > Create). It comes with a 90-day trial of most Enterprise features, which the
   custom `Workspace` object needs. A test account expires after 90 days without API calls.
   Running `pipelineos sync` regularly keeps it alive; otherwise renew it from the Test accounts
   page.
2. In the test account, create a **service key** (Development > Keys > Service keys). Since
   2026-09-28, new accounts can no longer create legacy private apps; service keys replace them.
   If the test account does not offer service keys, create a static-auth app in a developer
   project instead. Either way PipelineOS only needs a bearer token.
3. Give the key these scopes:

   | Scope | Used by |
   |---|---|
   | `crm.schemas.contacts.read` / `.write` | properties and groups on contacts |
   | `crm.schemas.companies.read` / `.write` | properties and groups on companies |
   | `crm.schemas.deals.read` / `.write` | properties, pipelines and stages on deals |
   | `crm.schemas.custom.read` / `.write` | the `Workspace` custom object |
   | `crm.objects.contacts.read` / `.write` | `load`, `sync`, `route-demos` |
   | `crm.objects.companies.read` / `.write` | `load`, `sync` |
   | `crm.objects.custom.read` / `.write` | `sync` (Workspace records) |

4. Find the portal id (the Hub ID in the account menu) and fill in `.env` from `.env.example`:

   ```bash
   PIPELINEOS_HUBSPOT_TOKEN=<service key>
   PIPELINEOS_ALLOWED_PORTAL=<hub id of the TEST account>
   ```

   `PIPELINEOS_ALLOWED_PORTAL` is the safety catch. Every write checks the token's portal id
   against it and refuses on a mismatch, so a key for a real company's portal cannot be written
   to by mistake, even with `--yes`.

## 2. Configure the portal

```bash
uv run pipelineos validate              # spec only, no API calls
uv run pipelineos plan                  # read the portal, show the diff; exit 2 = changes
uv run pipelineos apply                 # preview again
uv run pipelineos apply --yes           # write, then re-plan: expect 0 changes pending
```

`apply` never deletes. Anything in the portal that the spec does not mention is shown with `?`
and left alone. Changes HubSpot will not make in place (a property's type, its uniqueness, the
custom object's name or labels) are shown with `!` and a reason; do those by hand.

### Manual checklist

`plan` and `apply` print it. HubSpot has no API for these, so they are done once in the UI:

- **Stage-required properties**: for each deal stage, the properties listed in the spec
  (Settings > Objects > Deals > Pipelines > edit stage properties).
- **Permission sets** SDR, AE, CSM and RevOps admin. Only RevOps admin can edit
  `lifecyclestage`, `arr`, `is_pql`, `pql_score` and `company_key`.
- **Teams**: SDR mid-market, AE enterprise, Customer success.
- **Routing workflows**: one contact workflow per `routing_queue` value that rotates the owner
  within its team and creates a follow-up task due in 24 hours.
- **PQL workflow**: when a company's `is_pql` becomes true, set lifecycle stage to Marketing
  qualified lead, set lead status Open on its contacts and notify the owner. PipelineOS
  deliberately does not write lifecycle stage or lead status itself, so a nightly sync never
  overwrites a status a rep set.
- **Revenue dashboard**: lifecycle funnel conversion, pipeline by stage against target, days in
  stage, win rate by `deal_source`, PQL-to-opportunity conversion.

Screenshot each one into `docs/img/` once done.

## 3. Load the cleaned data

```bash
uv run pipelineos generate
uv run pipelineos clean
uv run pipelineos load            # preview: counts and calls
uv run pipelineos load --yes      # write; per-row failures are listed, not fatal
```

Running it twice is safe: the second run updates, it creates nothing.

## 4. Sync product usage

```bash
uv run pipelineos usage generate
uv run pipelineos usage score
uv run pipelineos sync            # preview
uv run pipelineos sync --yes      # write; state goes to data/usage/sync_state.json
```

The state file stores what was last sent per record. A second `sync --yes` with no new usage
sends nothing. If the state file is lost, delete it and run again: every upsert is idempotent,
so the only cost is one full re-send.

## 5. Route demo requests

```bash
ollama pull llama3.1:8b                       # optional; without it, keyword rules are used
PIPELINEOS_OLLAMA_URL=http://localhost:11434 uv run pipelineos route-demos --ollama
uv run pipelineos route-demos --ollama --yes
```

## 6. Confirm these on the first live run

The client and the fake follow HubSpot's 2026-09 API documentation. These details could not be
confirmed without a live portal. Each is isolated in one place, so a difference is a one-line fix.

| Check | Where to fix it |
|---|---|
| Service keys exist in developer test accounts | auth only; no code change |
| `GET /account-info/2026-09/details` returns `portalId` | `hubspot/paths.py` |
| Property-group path is `/crm/properties/2026-09/{object}/groups` | `hubspot/paths.py` |
| Shape of the validation-rule list response | `flatten_validation_rules` in `hubspot/http.py` |
| Deal-stage `metadata.isClosed` is writable | `stage_payload` in `portal/plan.py` |
| Date properties accept `YYYY-MM-DD` strings | `workspace_properties` in `usage/sync.py` |
| Which rate-limit tier test accounts get (assumed 100 requests per 10 s) | `BUCKET_CAPACITY` in `hubspot/http.py` |

## Recovery

| Situation | What to do |
|---|---|
| `apply` stopped partway | Fix the cause it printed, run `apply --yes` again. It continues from where it stopped. |
| Test account expired | Create a new one, update `.env`, run `apply --yes`. The YAML is the source of truth; the portal is disposable. |
| A bad `load` or `sync` | Nothing is deleted automatically. Fix the data and re-run; upserts overwrite in place. |
| Wrong portal in `.env` | Writes are refused until `PIPELINEOS_ALLOWED_PORTAL` matches the token's portal. |
