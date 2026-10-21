"""Workspace facts from events, and an explainable PQL score from those facts.

``aggregate`` is one O(e) pass over the events. ``score`` is a pure function of one workspace's
facts and the rules in ``portal/pql.yaml``: each rule that fires adds its points and its reason
sentence, so the number HubSpot shows a rep always comes with why.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipelineos.data.normalize import company_key, email_domain, is_freemail
from pipelineos.usage.events import Event

KEY_ACTIONS = frozenset({"ci_pipeline_connected", "flaky_test_quarantined", "api_key_created"})
PLAN_RANK = {"free": 0, "team": 1, "enterprise_trial": 2, "enterprise": 3}

Metric = Literal[
    "seats",
    "active_users_14d",
    "key_actions_14d",
    "ci_pipelines_connected",
    "api_keys_created",
    "days_since_active",
    "company_employees",
]


@dataclass
class WorkspaceFacts:
    workspace_id: str
    name: str = ""
    plan: str = "free"
    admin_email: str = ""
    members: set[str] = field(default_factory=set)
    user_ids: dict[str, str] = field(default_factory=dict)  # email -> product user id
    signup_date: date | None = None
    last_active_date: date | None = None
    active_users_14d: int = 0
    key_actions_14d: int = 0
    ci_pipelines_connected: int = 0
    api_keys_created: int = 0
    company_key: str | None = None  # the company most members' work emails point to

    @property
    def seats(self) -> int:
        return len(self.members)


def aggregate(events: list[Event], as_of: date, window_days: int = 14) -> dict[str, WorkspaceFacts]:
    window_start = as_of - timedelta(days=window_days)
    facts: dict[str, WorkspaceFacts] = {}
    active: dict[str, set[str]] = defaultdict(set)
    plan_at: dict[str, date] = {}
    for e in events:
        if e.at > as_of:
            continue  # events after the scoring date do not exist yet
        f = facts.setdefault(e.workspace_id, WorkspaceFacts(e.workspace_id))
        f.members.add(e.email)
        f.user_ids.setdefault(e.email, e.user_id)
        if e.type == "signup":
            f.name, f.admin_email, f.signup_date = e.value, e.email, e.at
        elif e.type == "plan_changed" and e.at >= plan_at.get(e.workspace_id, date.min):
            f.plan, plan_at[e.workspace_id] = e.value, e.at
        if e.type in ("session", *KEY_ACTIONS):
            if f.last_active_date is None or e.at > f.last_active_date:
                f.last_active_date = e.at
            if e.at > window_start:
                if e.type == "session":
                    active[e.workspace_id].add(e.email)
                else:
                    f.key_actions_14d += 1
        if e.type == "ci_pipeline_connected":
            f.ci_pipelines_connected += 1
        elif e.type == "api_key_created":
            f.api_keys_created += 1
    for workspace_id, users in active.items():
        facts[workspace_id].active_users_14d = len(users)
    for f in facts.values():
        f.company_key = _majority_company(f.members)
    return facts


def _majority_company(emails: set[str]) -> str | None:
    counts: dict[str, int] = defaultdict(int)
    for email in emails:
        if not is_freemail(email_domain(email)):
            key = company_key("", email)
            if key:
                counts[key] += 1
    if not counts:
        return None
    return min(counts, key=lambda k: (-counts[k], k))


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    reason: str
    metric: Metric
    gte: float | None = None
    lte: float | None = None
    points: int = Field(gt=0)

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        if (self.gte is None) == (self.lte is None):
            raise ValueError(f"rule {self.id}: set exactly one of gte / lte")
        return self

    def fires(self, value: float | None) -> bool:
        if value is None:
            return False
        if self.gte is not None:
            return value >= self.gte
        assert self.lte is not None
        return value <= self.lte


class PqlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: int = Field(ge=1, le=100)
    window_days: int = Field(ge=1)
    active_within_days: int = Field(ge=1)
    rules: list[Rule]

    @model_validator(mode="after")
    def _points_total_100(self) -> Self:
        total = sum(r.points for r in self.rules)
        if total != 100:
            raise ValueError(f"rule points must add up to 100, got {total}")
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("rule ids must be unique")
        return self


def load_pql_config(path: Path) -> PqlConfig:
    return PqlConfig.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class PqlResult:
    score: int
    is_pql: bool
    reasons: tuple[str, ...]

    @property
    def reasons_text(self) -> str:
        return "; ".join(self.reasons)


def metrics(
    facts: WorkspaceFacts, as_of: date, company_employees: int | None
) -> dict[str, float | None]:
    days_since = (as_of - facts.last_active_date).days if facts.last_active_date else None
    return {
        "seats": facts.seats,
        "active_users_14d": facts.active_users_14d,
        "key_actions_14d": facts.key_actions_14d,
        "ci_pipelines_connected": facts.ci_pipelines_connected,
        "api_keys_created": facts.api_keys_created,
        "days_since_active": days_since,
        "company_employees": company_employees,
    }


def score(
    facts: WorkspaceFacts, config: PqlConfig, as_of: date, company_employees: int | None = None
) -> PqlResult:
    values = metrics(facts, as_of, company_employees)
    days_since = values["days_since_active"]
    if days_since is None or days_since > config.active_within_days:
        return PqlResult(0, False, (f"No activity in the last {config.active_within_days} days",))
    fired = [r for r in config.rules if r.fires(values[r.metric])]
    total = sum(r.points for r in fired)
    return PqlResult(total, total >= config.threshold, tuple(r.reason for r in fired))


def best_plan(plans: list[str]) -> str:
    return max(plans, key=lambda p: PLAN_RANK.get(p, -1), default="free")
