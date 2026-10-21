"""Product usage events: the model, a seeded generator tied to the cleaned CRM data, and CSV IO.

The generator stands in for Quarry's product database. Workspaces belong to the companies in the
cleaned CRM export (members are that company's contacts, plus product-only users the CRM has never
seen), and follow one of five behaviours, so the scorer has something real to separate: hobbyists,
teams evaluating seriously, dormant signups, paying teams and enterprise accounts.
"""

from __future__ import annotations

import csv
import random
import string
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

EventType = Literal[
    "signup",
    "seat_added",
    "session",
    "ci_pipeline_connected",
    "flaky_test_quarantined",
    "api_key_created",
    "plan_changed",
]
EVENT_FIELDS = ("workspace_id", "user_id", "email", "type", "at", "value")
DEFAULT_AS_OF = date(2026, 10, 1)


@dataclass(frozen=True)
class Event:
    workspace_id: str
    user_id: str
    email: str
    type: EventType
    at: date
    value: str = ""  # workspace name on signup, plan name on plan_changed


@dataclass(frozen=True)
class Archetype:
    name: str
    weight: int
    users: tuple[int, int]
    plan: str
    active: bool  # sessions inside the scoring window
    sessions_per_user: tuple[int, int]
    ci: tuple[int, int]
    quarantines: tuple[int, int]
    api_keys: tuple[int, int]


ARCHETYPES = (
    Archetype("hobbyist", 45, (1, 1), "free", True, (1, 3), (0, 1), (0, 5), (0, 0)),
    Archetype("evaluating_team", 20, (3, 8), "free", True, (3, 8), (1, 3), (20, 60), (0, 2)),
    Archetype("dormant", 20, (1, 4), "free", False, (2, 6), (0, 1), (0, 10), (0, 1)),
    Archetype("paying_team", 10, (5, 15), "team", True, (4, 10), (2, 5), (30, 90), (1, 3)),
    Archetype("enterprise", 5, (10, 30), "enterprise", True, (5, 12), (3, 8), (60, 150), (2, 5)),
)


class _Generator:
    def __init__(self, seed: int, as_of: date) -> None:
        self.rng = random.Random(seed)
        self.as_of = as_of
        self.user_ids: dict[str, str] = {}
        self.events: list[Event] = []
        self.archetypes: dict[str, str] = {}

    def token(self, prefix: str, length: int) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return prefix + "".join(self.rng.choice(alphabet) for _ in range(length))

    def user_id(self, email: str) -> str:
        if email not in self.user_ids:
            self.user_ids[email] = self.token("usr_", 10)
        return self.user_ids[email]

    def day(self, start_days_ago: int, end_days_ago: int) -> date:
        return self.as_of - timedelta(days=self.rng.randint(end_days_ago, start_days_ago))

    def workspace(self, name: str, members: list[str], archetype: Archetype) -> None:
        workspace_id = self.token("ws_", 8)
        self.archetypes[workspace_id] = archetype.name
        signup = self.day(400, 20)
        admin = members[0]

        def emit(email: str, type_: EventType, at: date, value: str = "") -> None:
            self.events.append(Event(workspace_id, self.user_id(email), email, type_, at, value))

        emit(admin, "signup", signup, name)
        for email in members[1:]:
            emit(email, "seat_added", signup + timedelta(days=self.rng.randint(0, 15)))
        if archetype.plan != "free":
            emit(
                admin,
                "plan_changed",
                signup + timedelta(days=self.rng.randint(5, 30)),
                archetype.plan,
            )
        elif archetype.name == "evaluating_team" and self.rng.random() < 0.3:
            emit(admin, "plan_changed", self.day(20, 5), "enterprise_trial")

        if archetype.active:
            window = (13, 0)
        else:  # last seen 30 to 200 days ago, never before signing up
            window = (max(31, min(200, (self.as_of - signup).days)), 30)
        for email in members:
            for _ in range(self.rng.randint(*archetype.sessions_per_user)):
                emit(email, "session", max(signup, self.day(*window)))
        actions: list[tuple[EventType, tuple[int, int]]] = [
            ("ci_pipeline_connected", archetype.ci),
            ("flaky_test_quarantined", archetype.quarantines),
            ("api_key_created", archetype.api_keys),
        ]
        for type_, span in actions:
            for _ in range(self.rng.randint(*span)):
                emit(self.rng.choice(members), type_, max(signup, self.day(*window)))


def generate_events(
    contacts: list[dict[str, str]],
    companies: list[dict[str, str]],
    *,
    seed: int = 7,
    as_of: date = DEFAULT_AS_OF,
) -> tuple[list[Event], dict[str, str]]:
    """Events for workspaces built on the cleaned CRM data, plus each workspace's archetype.

    The archetype map is ground truth for evaluating the scorer; sync never reads it.
    """
    gen = _Generator(seed, as_of)
    by_company: dict[str, list[dict[str, str]]] = defaultdict(list)
    personal: list[dict[str, str]] = []
    for contact in contacts:
        if contact.get("company_key"):
            by_company[contact["company_key"]].append(contact)
        else:
            personal.append(contact)
    names = {c["company_key"]: c["name"] for c in companies}
    weights = [a.weight for a in ARCHETYPES]

    for key in sorted(by_company):
        people = sorted(c["email"] for c in by_company[key])
        if gen.rng.random() > 0.6:
            continue  # not every company in the CRM uses the product
        archetype = gen.rng.choices(ARCHETYPES, weights=weights)[0]
        wanted = gen.rng.randint(*archetype.users)
        members = gen.rng.sample(people, min(wanted, len(people)))
        members += [f"dev{n}.{gen.token('', 4)}@{key}" for n in range(wanted - len(members))]
        suffix = gen.rng.choice(("Platform", "CI", "Engineering", "Infra", "Mobile"))
        gen.workspace(f"{names.get(key, key)} {suffix}", members, archetype)

    for contact in sorted(personal, key=lambda c: c["email"]):
        if gen.rng.random() < 0.3:
            first = contact.get("firstname") or "My"
            gen.workspace(f"{first}'s workspace", [contact["email"]], ARCHETYPES[0])

    gen.events.sort(key=lambda e: (e.at, e.workspace_id, e.type, e.email))
    return gen.events, gen.archetypes


def write_events(events: list[Event], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(EVENT_FIELDS)
        for e in events:
            writer.writerow([e.workspace_id, e.user_id, e.email, e.type, e.at.isoformat(), e.value])


def read_events(path: Path) -> list[Event]:
    with path.open(newline="") as handle:
        return [
            Event(
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                email=row["email"],
                type=row["type"],  # type: ignore[arg-type]
                at=date.fromisoformat(row["at"]),
                value=row["value"],
            )
            for row in csv.DictReader(handle)
        ]
