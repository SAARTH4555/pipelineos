from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipelineos.usage.events import Event
from pipelineos.usage.score import (
    PqlConfig,
    WorkspaceFacts,
    aggregate,
    best_plan,
    load_pql_config,
    score,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_pql_config(ROOT / "portal" / "pql.yaml")
AS_OF = date(2026, 10, 1)


def ev(
    type_: str, days_ago: int, email: str = "ana@acme.io", value: str = "", workspace: str = "ws_1"
) -> Event:
    return Event(workspace, f"usr_{email}", email, type_, AS_OF - timedelta(days=days_ago), value)  # type: ignore[arg-type]


def test_aggregate_counts_window_and_plan() -> None:
    events = [
        ev("signup", 100, value="Acme CI"),
        ev("seat_added", 90, email="bo@acme.io"),
        ev("seat_added", 90, email="cy@gmail.com"),
        ev("plan_changed", 50, value="team"),
        ev("plan_changed", 10, value="enterprise_trial"),
        ev("session", 20),  # outside the 14-day window
        ev("session", 2),
        ev("session", 3, email="bo@acme.io"),
        ev("ci_pipeline_connected", 40),
        ev("flaky_test_quarantined", 5),
        ev("api_key_created", 1),
        ev("session", -3),  # after the scoring date: ignored
    ]
    f = aggregate(events, AS_OF)["ws_1"]
    assert (f.name, f.admin_email, f.plan) == ("Acme CI", "ana@acme.io", "enterprise_trial")
    assert f.seats == 3
    assert f.active_users_14d == 2
    assert f.key_actions_14d == 2  # quarantine + api key inside the window
    assert (f.ci_pipelines_connected, f.api_keys_created) == (1, 1)
    assert f.last_active_date == AS_OF - timedelta(days=1)
    assert f.company_key == "acme.io"  # 2 of 3 members are on acme.io


def test_freemail_only_workspace_has_no_company() -> None:
    f = aggregate([ev("signup", 30, email="dev@gmail.com")], AS_OF)["ws_1"]
    assert f.company_key is None


def facts(**overrides: object) -> WorkspaceFacts:
    base = WorkspaceFacts("ws_1", last_active_date=AS_OF)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# Each rule, exactly at its edge and one step outside it.
@pytest.mark.parametrize(
    ("rule", "fires", "misses", "employees"),
    [
        ("team_adoption", {"active_users_14d": 3}, {"active_users_14d": 2}, None),
        ("ci_connected", {"ci_pipelines_connected": 1}, {"ci_pipelines_connected": 0}, None),
        ("heavy_usage", {"key_actions_14d": 40}, {"key_actions_14d": 39}, None),
        (
            "invited_team",
            {"members": {f"u{i}" for i in range(8)}},
            {"members": {f"u{i}" for i in range(7)}},
            None,
        ),
        ("api_integration", {"api_keys_created": 1}, {"api_keys_created": 0}, None),
        (
            "active_this_week",
            {"last_active_date": AS_OF - timedelta(days=3)},
            {"last_active_date": AS_OF - timedelta(days=4)},
            None,
        ),
    ],
)
def test_each_rule_edge(
    rule: str, fires: dict[str, object], misses: dict[str, object], employees: int | None
) -> None:
    reason = next(r.reason for r in CONFIG.rules if r.id == rule)
    assert reason in score(facts(**fires), CONFIG, AS_OF, employees).reasons
    assert reason not in score(facts(**misses), CONFIG, AS_OF, employees).reasons


def test_company_fit_rule() -> None:
    reason = next(r.reason for r in CONFIG.rules if r.id == "company_fit")
    assert reason in score(facts(), CONFIG, AS_OF, 50).reasons
    assert reason not in score(facts(), CONFIG, AS_OF, 49).reasons
    assert reason not in score(facts(), CONFIG, AS_OF, None).reasons


def test_threshold_edge() -> None:
    # team (25) + ci (20) + active this week (10) = 55: not yet a PQL
    near = facts(active_users_14d=3, ci_pipelines_connected=1)
    result = score(near, CONFIG, AS_OF)
    assert (result.score, result.is_pql) == (55, False)
    # + company fit (10) = 65: crosses 60
    over = score(near, CONFIG, AS_OF, company_employees=200)
    assert (over.score, over.is_pql) == (65, True)
    assert over.reasons_text.count("; ") == 3


def test_inactive_workspace_scores_zero_whatever_it_did() -> None:
    stale = facts(
        active_users_14d=9,
        ci_pipelines_connected=4,
        api_keys_created=2,
        last_active_date=AS_OF - timedelta(days=15),
    )
    result = score(stale, CONFIG, AS_OF, 1000)
    assert (result.score, result.is_pql) == (0, False)
    assert result.reasons == ("No activity in the last 14 days",)
    assert score(facts(last_active_date=None), CONFIG, AS_OF).score == 0


def test_config_validation() -> None:
    rules = [r.model_dump() for r in CONFIG.rules]
    with pytest.raises(ValidationError, match="add up to 100"):
        PqlConfig.model_validate(
            {"threshold": 60, "window_days": 14, "active_within_days": 14, "rules": rules[:-1]}
        )
    with pytest.raises(ValidationError, match="unique"):
        dupe = [*rules[:-1], {**rules[-1], "id": rules[0]["id"]}]
        PqlConfig.model_validate(
            {"threshold": 60, "window_days": 14, "active_within_days": 14, "rules": dupe}
        )
    with pytest.raises(ValidationError, match="exactly one"):
        PqlConfig.model_validate(
            {
                "threshold": 60,
                "window_days": 14,
                "active_within_days": 14,
                "rules": [{**rules[0], "lte": 1}],
            }
        )


def test_best_plan() -> None:
    assert best_plan(["free", "enterprise_trial", "team"]) == "enterprise_trial"
    assert best_plan([]) == "free"
