# Run it yourself

About 30 minutes, start to finish. At the end you have your own HubSpot portal, set up and
filled by PipelineOS, that you can open in your browser and show to anyone.

You need a Mac, Linux or Windows (with WSL) terminal. Nothing is paid.

## 1. Install and try it with no accounts (5 minutes)

```bash
# 1. Install uv, the Python tool the project uses (it installs Python 3.12 for you)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Get the code
git clone https://github.com/SAARTH4555/pipelineos.git
cd pipelineos
uv sync --group dev

# 3. Run everything against a pretend HubSpot that lives in memory
uv run pipelineos plan --fake            # what an empty portal needs: "Plan: 42 to create"
uv run pipelineos apply --fake --yes     # builds it, then checks: "0 changes pending"
uv run pipelineos generate               # a messy CRM import with known duplicates
uv run pipelineos clean                  # cleans it: "quality 78.4 -> 99.8"
uv run pipelineos usage generate
uv run pipelineos usage score            # which product workspaces are ready for sales, and why
uv run pipelineos route-demos --fake     # routes 6 demo requests to the right sales queue
```

If those commands print what the comments say, the project works on your machine.

## 2. Get your own HubSpot test portal (10 minutes)

1. Go to [developers.hubspot.com](https://developers.hubspot.com/) and create a free developer
   account.
2. Inside it, open **Test accounts** and click **Create test account**. It's free, comes with
   a 90-day trial of the Enterprise features PipelineOS needs, and is separate from any real
   company's data.
3. Open the test account. Go to **Development > Keys > Service keys** and create a key. Tick
   the read and write scopes for contacts, companies, deals and custom objects, both
   "objects" and "schemas" (the full list is in [`runbook.md`](runbook.md#1-get-a-test-portal-and-a-key)).
   Copy the key.
4. Find the account's **Hub ID**: click the account name in the top-right corner.
5. In the `pipelineos` folder, create a file called `.env`:

   ```
   PIPELINEOS_HUBSPOT_TOKEN=<the service key>
   PIPELINEOS_ALLOWED_PORTAL=<the Hub ID>
   ```

   The second line is a safety catch: PipelineOS refuses to write to any other portal.

## 3. Build your portal with PipelineOS (10 minutes)

```bash
uv run pipelineos plan                 # reads your portal, shows what it will add
uv run pipelineos apply --yes          # adds it; ends with "0 changes pending"
uv run pipelineos load --yes           # loads the cleaned companies and contacts
uv run pipelineos sync --yes           # adds product workspaces and PQL scores
uv run pipelineos route-demos --yes    # writes the routing onto 6 demo contacts
```

Nothing is ever deleted: every command previews first and only adds or updates.

## 4. What to open in HubSpot

| Where in HubSpot | What you'll see |
|---|---|
| Settings > Properties | New groups: Lead management, Account and PQL, Deal qualification |
| Contacts, Companies | The cleaned import, with `company_key`, PQL score and reasons |
| Custom objects > Workspaces | One record per product workspace, linked to its company and people |
| Deals > Pipelines | "New Business" and "Expansion", with their stages |
| A contact from step 3 | Its routing queue and the one-line reason it went there |

`apply` also prints a short **manual checklist**: things HubSpot only allows in its UI, such as
required fields per deal stage, teams, workflows and the dashboard. Doing those by hand is the
last 20 minutes of a real setup.

## If something fails

This is the first run against a live portal, so a HubSpot API detail may differ from its
documentation. [`runbook.md`](runbook.md#6-confirm-these-on-the-first-live-run) lists the likely
spots. Copy the full error message; it names the exact call that failed.
