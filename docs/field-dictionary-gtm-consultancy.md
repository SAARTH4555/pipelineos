# Field dictionary: A GTM-ops consultancy (generic)

Generated from `portal/gtm-consultancy.yaml` by `pipelineos dictionary`. Do not edit by hand.

A consultancy that sets up GTM systems for Series A-C B2B startups. Its pipeline is built from public signals: a startup hiring its first RevOps or CRM admin role, often right after a raise. Reps need to see why each account is on their list, and to know which CRM the prospect runs before the first call.

## Contacts

Property groups: `pipelineos_prospect` (Prospecting)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `signup_source` | Source | enumeration / select | pipelineos_prospect | How the contact entered the CRM. Options: `signal_outbound`, `referral`, `inbound`, `event`, `partner`. |
| `persona` | Persona | enumeration / select | pipelineos_prospect | Buying role at the prospect. Options: `revops_leader`, `sales_leader`, `founder_exec`, `operator`, `other`. |

## Companies

Property groups: `pipelineos_signals` (Account signals)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `company_key` | Company key | string / text | pipelineos_signals | **Unique; upsert key.** Normalized registrable domain (acme.io). Upsert key, shared with SignalScout. Validation: DOMAIN; FORMAT LOWER. |
| `account_segment` | Account segment | enumeration / select | pipelineos_signals | From employee count: under 50 smb, 50-499 mid_market, 500+ enterprise. Options: `smb`, `mid_market`, `enterprise`. |
| `signal_tier` | Signal tier | enumeration / select | pipelineos_signals | SignalScout's tier. A = an open GTM-ops role plus enough fit and intent to call now. Options: `A`, `B`, `C`. |
| `signal_score` | Signal score | number / number | pipelineos_signals | SignalScout's 0-100 score (fit + intent, decayed with signal age). Validation: MIN_NUMBER 0; MAX_NUMBER 100. |
| `signal_reasons` | Why now | string / textarea | pipelineos_signals | Every scoring rule that fired, in plain sentences. What a rep reads first. |
| `signal_best_role` | GTM-ops opening | string / text | pipelineos_signals | The open role that scored highest, e.g. "Revenue Operations Manager". |
| `signal_crm` | CRM in use | enumeration / select | pipelineos_signals | From the prospect's job postings first, its website scripts second. Options: `salesforce`, `hubspot`, `both`. |
| `signal_funding_date` | Last raise | date / date | pipelineos_signals | Date of first sale on the latest SEC Form D. |
| `signal_funding_amount` | Last raise (USD) | number / number | pipelineos_signals | Amount sold on the latest SEC Form D. Validation: MIN_NUMBER 0. |
| `signal_checked_at` | Signals checked | date / date | pipelineos_signals | When SignalScout last scored the account. Stale after 30 days. |

## Deals

Property groups: `pipelineos_engagement` (Engagement)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `engagement_type` | Engagement type | enumeration / select | pipelineos_engagement | What the client is buying. Options: `crm_setup`, `crm_migration`, `data_cleanup`, `routing_automation`, `reporting`, `retainer`. |
| `entry_signal` | Entry signal | enumeration / select | pipelineos_engagement | The signal that opened the conversation. Drives win-rate-by-signal reporting. Options: `gtm_ops_hire`, `funding`, `new_sales_leader`, `inbound`, `referral`. |

## Pipeline: Engagements (deals)

| Stage id | Label | Probability | Closed | Required to enter (set in UI) |
|---|---|---|---|---|
| `en_discovery` | Discovery call | 10% | no | `entry_signal` |
| `en_audit` | Systems audit | 30% | no | `engagement_type` |
| `en_proposal` | Proposal | 60% | no | `amount`, `closedate` |
| `en_closed_won` | Closed won | 100% | yes | `amount` |
| `en_closed_lost` | Closed lost | 0% | yes | none |

## Association labels

| From | To | Name | Label | Inverse label |
|---|---|---|---|---|
| contacts | companies | `economic_buyer` | Economic buyer | Has economic buyer |
| contacts | companies | `revops_owner` | RevOps owner | Has RevOps owner |

## Lifecycle stages

| Stage | Definition | Entry criteria | Owner |
|---|---|---|---|
| Lead | A contact at a scored account. | Created by Clay or by hand for a Tier-A or Tier-B account. | SDR |
| Sales qualified lead | Confirmed a GTM-ops problem on a call. | Discovery call held and the problem written down. | Account executive |
| Opportunity | An Engagements deal exists. | Deal created at Discovery call. | Account executive |
| Customer | A signed engagement. | Deal moves to Closed won. | Engagement lead |

## Manual steps (HubSpot UI only)

| Area | Step | Why |
|---|---|---|
| Pipelines | Set the required properties for each Engagements stage, as listed per stage in this spec. | HubSpot has no API for stage-required properties. |
| Workflows | Build a company workflow that, when signal_tier becomes A, assigns an owner by round robin and creates a call task due in 2 days with signal_reasons in its notes. | The signal is only worth something while it is fresh; the reasons give the rep the opening line. |
| Workflows | Build a company workflow that flags accounts whose signal_checked_at is older than 30 days for a re-run of SignalScout. | Hiring and funding signals decay; stale tiers mislead reps. |
| Reporting | Build the Signals dashboard: accounts by tier, Tier-A accounts contacted within 2 days, win rate by entry_signal, pipeline by engagement_type. | Shows which signals actually turn into revenue, so the scoring weights can be tuned. |
