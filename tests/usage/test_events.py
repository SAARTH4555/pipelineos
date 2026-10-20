from __future__ import annotations

from collections import Counter
from pathlib import Path

from pipelineos.usage.events import DEFAULT_AS_OF, generate_events, read_events, write_events
from pipelineos.usage.score import aggregate

CONTACTS = [
    {"email": f"p{i}@c{i % 20}.io", "company_key": f"c{i % 20}.io", "firstname": "A"}
    for i in range(200)
] + [{"email": f"solo{i}@gmail.com", "company_key": "", "firstname": "Sol"} for i in range(30)]
COMPANIES = [{"company_key": f"c{i}.io", "name": f"C{i}"} for i in range(20)]


def test_generation_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    a, arch_a = generate_events(CONTACTS, COMPANIES, seed=3)
    b, arch_b = generate_events(CONTACTS, COMPANIES, seed=3)
    assert a == b and arch_a == arch_b
    write_events(a, tmp_path / "events.csv")
    assert read_events(tmp_path / "events.csv") == a


def test_every_workspace_has_one_signup_and_sorted_events() -> None:
    events, archetypes = generate_events(CONTACTS, COMPANIES, seed=5)
    signups = Counter(e.workspace_id for e in events if e.type == "signup")
    assert set(signups) == set(archetypes) and set(signups.values()) == {1}
    assert events == sorted(events, key=lambda e: (e.at, e.workspace_id, e.type, e.email))
    user_ids: dict[str, str] = {}
    for e in events:
        assert user_ids.setdefault(e.email, e.user_id) == e.user_id  # one id per person
        assert e.user_id.startswith("usr_") and e.workspace_id.startswith("ws_")


def test_dormant_workspaces_have_no_recent_activity() -> None:
    contacts = [
        {"email": f"p{i}@d{i % 100}.io", "company_key": f"d{i % 100}.io"} for i in range(600)
    ]
    companies = [{"company_key": f"d{i}.io", "name": f"D{i}"} for i in range(100)]
    events, archetypes = generate_events(contacts, companies, seed=11)
    facts = aggregate(events, DEFAULT_AS_OF)
    dormant = [w for w, a in archetypes.items() if a == "dormant"]
    assert len(dormant) >= 5
    for w in dormant:
        last = facts[w].last_active_date
        assert last is not None and (DEFAULT_AS_OF - last).days >= 20
