# Field dictionary: Quarry (fictional)

Generated from `portal/pipelineos.yaml` by `pipelineos dictionary`. Do not edit by hand.

Series A CI test-analytics platform, ~40 people. Engineers sign up free and invite their team; Team plan is self-serve; Enterprise (SSO, audit log, on-prem runners) is sold by a 3-person sales team. The build has to tell sales which free workspaces are worth a call, and route demo requests without a human reading every form.

## Contacts

Property groups: `pipelineos_lead` (Lead management)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `product_user_id` | Product user ID | string / text | pipelineos_lead | **Unique; upsert key.** The user's id in Quarry's product database. Upsert key for product-sourced contacts. Validation: REGEX `^usr_[a-z0-9]{8,}$`. |
| `signup_source` | Signup source | enumeration / select | pipelineos_lead | First-touch channel recorded by the product at signup. Options: `organic`, `github_marketplace`, `paid_search`, `referral`, `event`, `outbound`. |
| `demo_request_text` | Demo request (free text) | string / textarea | pipelineos_lead | Answer to "What are you hoping Quarry will fix?" on the demo form. Input to the classifier. |
| `demo_use_case` | Demo use case | enumeration / select | pipelineos_lead | Set by the demo-form classifier from demo_request_text. Options: `flaky_tests`, `slow_ci`, `test_visibility`, `compliance`, `other`. |
| `segment` | Segment | enumeration / select | pipelineos_lead | Sales segment used for routing. Derived from company size and the form answer. Options: `self_serve`, `mid_market`, `enterprise`. |
| `routing_queue` | Routing queue | enumeration / select | pipelineos_lead | Queue a HubSpot rotation workflow assigns an owner from. Written by PipelineOS. Options: `ae_enterprise`, `sdr_mid_market`, `self_serve_nurture`. |
| `routing_reason` | Routing reason | string / text | pipelineos_lead | One line explaining why the contact went to its queue, so reps can trust it. |
| `persona` | Persona | enumeration / select | pipelineos_lead | Buying role, from job title. Options: `engineering_leader`, `individual_engineer`, `platform_devops`, `executive`, `other`. |

## Companies

Property groups: `pipelineos_account` (Account and PQL)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `company_key` | Company key | string / text | pipelineos_account | **Unique; upsert key.** Normalized registrable domain (acme.io). Upsert key for companies, because HubSpot's own domain property is not unique and cannot be used to upsert. Validation: DOMAIN; FORMAT LOWER. |
| `account_segment` | Account segment | enumeration / select | pipelineos_account | From employee count: under 50 smb, 50-499 mid_market, 500+ enterprise. Options: `smb`, `mid_market`, `enterprise`. |
| `product_plan` | Product plan | enumeration / select | pipelineos_account | Highest plan across the company's workspaces. Options: `free`, `team`, `enterprise_trial`, `enterprise`. |
| `workspace_count` | Workspaces | number / number | pipelineos_account | Number of product workspaces linked to the company. |
| `is_pql` | Product-qualified | bool / booleancheckbox | pipelineos_account | True when any of the company's workspaces is product-qualified. |
| `pql_score` | PQL score | number / number | pipelineos_account | Highest workspace PQL score (0-100). Validation: MIN_NUMBER 0; MAX_NUMBER 100. |
| `pql_reasons` | PQL reasons | string / textarea | pipelineos_account | The scoring rules that fired for the top workspace, in plain words. |
| `pql_since` | PQL since | date / date | pipelineos_account | Date the company first became product-qualified. |

## Deals

Property groups: `pipelineos_deal` (Deal qualification)

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `deal_source` | Deal source | enumeration / select | pipelineos_deal | How the opportunity started. Drives source-attribution reporting. Options: `pql`, `inbound_demo`, `outbound`, `partner`, `expansion`. |
| `primary_use_case` | Primary use case | enumeration / select | pipelineos_deal | The problem the buyer is paying to solve. Options: `flaky_tests`, `slow_ci`, `test_visibility`, `compliance`, `other`. |
| `seats_committed` | Seats committed | number / number | pipelineos_deal | Seats in the proposal. Validation: MIN_NUMBER 1. |
| `arr` | ARR (USD) | number / number | pipelineos_deal | Annual recurring revenue of the deal, in USD. Validation: MIN_NUMBER 0. |
| `security_review_status` | Security review | enumeration / select | pipelineos_deal | Enterprise buyers run a vendor security review before signing. Options: `not_started`, `in_progress`, `approved`. |
| `competitor` | Main alternative | enumeration / select | pipelineos_deal | What the buyer would do instead of Quarry. Options: `none`, `in_house`, `other_vendor`. |
| `close_reason` | Close reason | enumeration / select | pipelineos_deal | Required on both closed stages, so win/loss analysis has data. Options: `won_product_fit`, `won_price`, `lost_price`, `lost_competitor`, `lost_no_decision`, `lost_timing`. |
| `next_step` | Next step | string / text | pipelineos_deal | The agreed next action and its date. |

## Custom object: Workspace (`workspace`)

A team's account in Quarry's product. The unit that becomes product-qualified.

- Primary display: `workspace_name`
- Required: `workspace_id`, `workspace_name`
- Searchable: `workspace_id`, `workspace_name`
- Associated with: contacts, companies

| Property | Label | Type | Group | Notes |
|---|---|---|---|---|
| `workspace_id` | Workspace ID | string / text | workspace_information | **Unique; upsert key.** The workspace's id in the product. Upsert key. Validation: REGEX `^ws_[a-z0-9]{6,}$`. |
| `workspace_name` | Workspace name | string / text | workspace_information | Name the team gave the workspace. |
| `plan` | Plan | enumeration / select | workspace_information | Current billing plan. Options: `free`, `team`, `enterprise_trial`, `enterprise`. |
| `seats` | Seats | number / number | workspace_information | Members invited to the workspace. |
| `active_users_14d` | Active users (14 days) | number / number | workspace_information | Members with at least one session in the last 14 days. |
| `key_actions_14d` | Key actions (14 days) | number / number | workspace_information | CI pipelines connected, flaky tests quarantined and API keys created in the last 14 days. |
| `signup_date` | Signup date | date / date | workspace_information | Date the workspace was created. |
| `last_active_date` | Last active | date / date | workspace_information | Most recent session by any member. |
| `pql_score` | PQL score | number / number | workspace_information | 0-100, from the rules in portal/pql.yaml. |
| `is_pql` | Product-qualified | bool / booleancheckbox | workspace_information | pql_score at or above the threshold in portal/pql.yaml. |
| `pql_reasons` | PQL reasons | string / textarea | workspace_information | The scoring rules that fired, in plain words. |

## Pipeline: New Business (deals)

| Stage id | Label | Probability | Closed | Required to enter (set in UI) |
|---|---|---|---|---|
| `nb_discovery` | Discovery | 10% | no | `deal_source`, `primary_use_case` |
| `nb_technical_eval` | Technical evaluation | 30% | no | `seats_committed` |
| `nb_security_review` | Security review | 50% | no | `security_review_status` |
| `nb_proposal` | Proposal | 70% | no | `arr`, `amount` |
| `nb_negotiation` | Negotiation | 85% | no | `closedate`, `next_step` |
| `nb_closed_won` | Closed won | 100% | yes | `close_reason`, `arr` |
| `nb_closed_lost` | Closed lost | 0% | yes | `close_reason`, `competitor` |

## Pipeline: Expansion (deals)

| Stage id | Label | Probability | Closed | Required to enter (set in UI) |
|---|---|---|---|---|
| `ex_identified` | Identified | 20% | no | `deal_source` |
| `ex_scoping` | Scoping | 50% | no | `seats_committed` |
| `ex_proposal` | Proposal | 75% | no | `arr`, `amount` |
| `ex_closed_won` | Closed won | 100% | yes | `close_reason`, `arr` |
| `ex_closed_lost` | Closed lost | 0% | yes | `close_reason` |

## Association labels

| From | To | Name | Label | Inverse label |
|---|---|---|---|---|
| workspace | contacts | `workspace_admin` | Admin | Admin of |
| workspace | contacts | `workspace_member` | Member | Member of |
| contacts | companies | `economic_buyer` | Economic buyer | Has economic buyer |
| contacts | companies | `champion` | Champion | Has champion |

## Lifecycle stages

| Stage | Definition | Entry criteria | Owner |
|---|---|---|---|
| Lead | Any contact we can email. | Product signup, form fill or import. | Marketing |
| Marketing qualified lead | Asked to talk to us, or engaged enough that sales should look. | Demo request, or a workspace becomes product-qualified (is_pql on its company). | SDR |
| Sales qualified lead | SDR confirmed fit and a real problem on a call. | SDR sets lead status Connected and books a discovery call. | SDR |
| Opportunity | A deal exists in New Business or Expansion. | Deal created (Discovery stage). | AE |
| Customer | Paying for Team or Enterprise. | Deal moves to Closed won, or a self-serve Team subscription starts. | CSM |

## Manual steps (HubSpot UI only)

| Area | Step | Why |
|---|---|---|
| Pipelines | Set the required properties for each deal stage (Settings > Objects > Deals > Pipelines > Edit stage properties), as listed per stage in this spec. | HubSpot has no API for stage-required properties. |
| Permissions | Create permission sets SDR, AE, CSM and RevOps admin. Only RevOps admin can edit lifecyclestage, arr, is_pql, pql_score and company_key. | Permission sets are not configurable through the API. Field-level edit locks keep PQL and revenue fields trustworthy. |
| Teams | Create teams SDR mid-market, AE enterprise and Customer success, and add users. | Rotation workflows assign owners by team. |
| Workflows | Build a contact workflow per routing_queue value that rotates the owner within the matching team and creates a follow-up task due in 24 hours. | The workflows API is beta and replaces the whole workflow on every update, so routing logic lives in PipelineOS and HubSpot only rotates owners. |
| Workflows | Build a company workflow that, when is_pql becomes true, sets lifecycle stage to Marketing qualified lead, sets lead status Open on associated contacts and notifies the owner. | PQL is a property, not a lifecycle stage, so HubSpot can act on it while staying reversible. |
| Reporting | Build the Revenue dashboard: lifecycle funnel conversion, pipeline by stage vs target, days in stage, win rate by deal_source, PQL-to-opportunity conversion. | Dashboards and reports cannot be created through the API. |
