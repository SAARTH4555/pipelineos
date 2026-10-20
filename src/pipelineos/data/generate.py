"""Seeded generator for a realistically messy CRM import, with ground truth kept separately.

It produces what a Series A startup's HubSpot usually looks like before a RevOps cleanup:
the same person entered several ways (case and whitespace noise, Gmail dot/plus aliases, a second
work address with a nickname, a personal address that shares a phone number), companies spelled
three ways with websites in every format, phone numbers in five formats, and some rows that are
not contacts at all.

Ground truth (which rows are the same person, which defects each row carries) goes to separate
``truth_*.csv`` files. Cleaning code reads only ``contacts.csv`` and ``companies.csv``; only the
evaluation reads the truth. Same config and seed give byte-identical files.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict

from faker import Faker

FREEMAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com")
TLDS = ("io", "dev", "com", "ai", "co")
INDUSTRIES = ("fintech", "devtools", "healthtech", "ecommerce", "security", "data", "edtech")
SOURCES = ("organic", "github_marketplace", "paid_search", "referral", "event", "outbound")
TITLES = (
    "Software Engineer",
    "Senior Software Engineer",
    "Staff Engineer",
    "Engineering Manager",
    "VP of Engineering",
    "CTO",
    "DevOps Engineer",
    "Platform Engineer",
    "QA Lead",
    "Head of Developer Productivity",
)
NICKNAMES = {
    "John": "Jon",
    "Michael": "Mike",
    "William": "Will",
    "Robert": "Rob",
    "Katherine": "Kate",
    "Elizabeth": "Liz",
    "Christopher": "Chris",
    "Daniel": "Dan",
    "Jennifer": "Jen",
    "Matthew": "Matt",
    "Andrew": "Andy",
    "Alexander": "Alex",
    "Samuel": "Sam",
    "Benjamin": "Ben",
    "Joseph": "Joe",
    "Thomas": "Tom",
    "Nicholas": "Nick",
    "Jessica": "Jess",
    "Rebecca": "Becca",
    "Anthony": "Tony",
}
JUNK_EMAILS = ("n/a", "test@test", "none", "asdf@asdf", "", "noreply@")

CONTACT_FIELDS = (
    "row_id",
    "email",
    "first_name",
    "last_name",
    "job_title",
    "phone",
    "company_name",
    "company_website",
    "signup_source",
    "created_at",
)
COMPANY_FIELDS = ("row_id", "name", "website", "employees", "industry")


@dataclass(frozen=True)
class GenConfig:
    n: int = 3000  # distinct real people
    dirty_fraction: float = 0.2
    seed: int = 7


@dataclass
class Company:
    key: str  # canonical registrable domain: the ground-truth company id
    name: str
    employees: int
    industry: str


@dataclass
class Person:
    entity_id: str
    first: str
    last: str
    title: str
    email: str
    phone: str  # canonical E.164, or "" if unknown
    company: Company
    source: str
    created: date


class Summary(TypedDict):
    config: dict[str, float | int]
    contact_rows: int
    people: int
    duplicate_rows: int
    duplicates_by_kind: dict[str, int]
    rows_with_defect: dict[str, int]
    company_rows: int
    companies: int


@dataclass
class Dataset:
    contacts: list[dict[str, str]] = field(default_factory=list)
    contact_truth: list[dict[str, str]] = field(default_factory=list)
    companies: list[dict[str, str]] = field(default_factory=list)
    company_truth: list[dict[str, str]] = field(default_factory=list)


class _Generator:
    def __init__(self, config: GenConfig) -> None:
        self.config = config
        self.rng = random.Random(config.seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(config.seed)
        self.phones: set[str] = set()
        self.emails: set[str] = set()
        self.data = Dataset()
        self.defects: Counter[str] = Counter()
        self.duplicates: Counter[str] = Counter()

    # -- clean entities --------------------------------------------------------------------

    def companies(self) -> list[Company]:
        n = max(5, self.config.n // 8)
        taken: set[str] = set()
        out: list[Company] = []
        while len(out) < n:
            base = self.fake.last_name().lower() + self.rng.choice(
                ("", "labs", "hq", "ly", "io", "stack", "works")
            )
            key = f"{base}.{self.rng.choice(TLDS)}"
            if key in taken:
                continue
            taken.add(key)
            employees = max(2, int(self.rng.lognormvariate(4.0, 1.3)))
            out.append(Company(key, base.capitalize(), employees, self.rng.choice(INDUSTRIES)))
        return out

    def phone(self) -> str:
        while True:
            # 555-01XX numbers are reserved for fiction; area codes vary to keep them unique.
            candidate = f"+1{self.rng.randint(201, 989)}55501{self.rng.randint(0, 99):02d}"
            if candidate not in self.phones:
                self.phones.add(candidate)
                return candidate

    def primary_email(self, first: str, last: str, company: Company) -> str:
        """A mailbox no other generated person has: two people never share one address."""
        fn, ln = first.lower(), last.lower()
        if self.rng.random() < 0.15:
            start = self.rng.randint(1, 99)
            candidates = [f"{fn}{ln}{n}@gmail.com" for n in range(start, 1000)]
        else:
            first_pattern = self.rng.choice((f"{fn}.{ln}", f"{fn[0]}{ln}", fn))
            fallbacks = [f"{fn}.{ln}", f"{fn[0]}{ln}", f"{fn}{ln[0]}"]
            fallbacks += [f"{fn}.{ln}{n}" for n in range(2, 100)]
            candidates = [f"{local}@{company.key}" for local in [first_pattern, *fallbacks]]
        email = next(c for c in candidates if c not in self.emails)
        self.emails.add(email)
        return email

    def person(self, i: int, company: Company) -> Person:
        first = self.rng.choice(
            [*NICKNAMES] if self.rng.random() < 0.35 else [self.fake.first_name()]
        )
        last = self.fake.last_name()
        email = self.primary_email(first, last, company)
        return Person(
            entity_id=f"p{i:05d}",
            first=first,
            last=last,
            title=self.rng.choice(TITLES),
            email=email,
            phone=self.phone() if self.rng.random() < 0.7 else "",
            company=company,
            source=self.rng.choice(SOURCES),
            created=date(2025, 1, 1) + timedelta(days=self.rng.randint(0, 630)),
        )

    # -- noise -------------------------------------------------------------------------------

    def dirty(self) -> bool:
        return self.rng.random() < self.config.dirty_fraction

    def noisy_email(self, email: str, defects: list[str]) -> str:
        if not self.dirty():
            return email
        defects.append("email_case_space")
        styled = self.rng.choice((email.upper(), email.title(), email))
        return self.rng.choice(("", " ", "  ")) + styled + self.rng.choice(("", " "))

    def noisy_name(self, name: str, defects: list[str]) -> str:
        if not self.dirty():
            return name
        defects.append("name_case_space")
        return self.rng.choice((name.upper(), name.lower(), f" {name} ", f"{name}  "))

    def phone_format(self, phone: str, defects: list[str]) -> str:
        if not phone:
            return ""
        if not self.dirty():
            return phone
        defects.append("phone_format")
        area, mid, last = phone[2:5], phone[5:8], phone[8:]
        return self.rng.choice(
            (
                f"({area}) {mid}-{last}",
                f"{area}.{mid}.{last}",
                f"{area}-{mid}-{last}",
                f"+1 {area} {mid} {last}",
                f"1{area}{mid}{last}",
            )
        )

    def website(self, key: str, defects: list[str]) -> str:
        if not self.dirty():
            return key
        if self.rng.random() < 0.3:
            defects.append("missing_website")
            return ""
        defects.append("website_format")
        return self.rng.choice(
            (f"https://www.{key}/", f"http://{key}", f"www.{key}", f"https://{key}/pricing")
        )

    def company_name(self, name: str, defects: list[str]) -> str:
        if not self.dirty():
            return name
        defects.append("company_name_variant")
        variants = (f"{name} Inc.", f"{name.upper()}, Inc", f"{name} LLC", name.lower())
        return self.rng.choice(variants)

    # -- rows -------------------------------------------------------------------------------

    def contact_row(self, p: Person, email: str, first: str, defects: list[str]) -> None:
        row_id = f"c{len(self.data.contacts) + 1:06d}"
        self.data.contacts.append(
            {
                "row_id": row_id,
                "email": self.noisy_email(email, defects),
                "first_name": self.noisy_name(first, defects),
                "last_name": self.noisy_name(p.last, defects),
                "job_title": p.title,
                "phone": self.phone_format(p.phone, defects),
                "company_name": self.company_name(p.company.name, defects),
                "company_website": self.website(p.company.key, defects),
                "signup_source": p.source,
                "created_at": p.created.isoformat(),
            }
        )
        if email.split("@")[-1] in FREEMAIL:
            defects.append("freemail")
        self.data.contact_truth.append(
            {"row_id": row_id, "entity_id": p.entity_id, "defects": ";".join(sorted(set(defects)))}
        )
        self.defects.update(set(defects))

    def duplicate(self, p: Person) -> None:
        kind = self.rng.choices(
            ("exact", "email_variant", "gmail_alias", "alt_work_email", "personal_email"),
            weights=(25, 25, 10, 25, 15),
        )[0]
        domain = p.email.split("@")[1]
        if kind == "gmail_alias" and domain != "gmail.com":
            kind = "alt_work_email"
        if kind == "personal_email" and not p.phone:
            kind = "alt_work_email"
        if kind == "alt_work_email" and domain in FREEMAIL:
            kind = "email_variant"
        self.duplicates[kind] += 1
        defects = [f"dup_{kind}"]
        if kind == "exact":
            self.data.contacts.append({**self.data.contacts[-1]})
            self.data.contacts[-1]["row_id"] = f"c{len(self.data.contacts):06d}"
            truth = self.data.contact_truth[-1]
            # keep the copied row's format defects; it is an exact duplicate, not another kind
            defects += [d for d in truth["defects"].split(";") if d and not d.startswith("dup_")]
            self.data.contact_truth.append(
                {
                    "row_id": self.data.contacts[-1]["row_id"],
                    "entity_id": p.entity_id,
                    "defects": ";".join(sorted(set(defects))),
                }
            )
            self.defects.update(set(defects))
            return
        first, email = p.first, p.email
        if kind == "email_variant":
            email = p.email.upper() if self.rng.random() < 0.5 else f" {p.email.title()} "
        elif kind == "gmail_alias":
            local = p.email.split("@")[0]
            cut = self.rng.randint(1, max(1, len(local) - 1))
            tag = self.rng.choice(("quarry", "ci", "work"))
            email = f"{local[:cut]}.{local[cut:]}+{tag}@gmail.com"
        elif kind == "alt_work_email":
            first = NICKNAMES.get(p.first, p.first[:-1] if len(p.first) > 4 else p.first)
            fn, ln = first.lower(), p.last.lower()
            local = self.rng.choice((f"{fn}.{ln}", f"{fn[0]}{ln}"))
            if f"{local}@{domain}" == p.email:
                local = f"{p.first.lower()}{p.last.lower()[0]}"
            email = f"{local}@{domain}"
        elif kind == "personal_email":
            number, provider = self.rng.randint(100, 999), self.rng.choice(FREEMAIL)
            email = f"{p.first.lower()}.{p.last.lower()}{number}@{provider}"
        self.contact_row(p, email, first, defects)

    def junk(self, i: int) -> None:
        row_id = f"c{len(self.data.contacts) + 1:06d}"
        self.data.contacts.append(
            {
                "row_id": row_id,
                "email": self.rng.choice(JUNK_EMAILS),
                "first_name": self.rng.choice(("test", "Test", "", "asdf")),
                "last_name": self.rng.choice(("", "test", "user")),
                "job_title": "",
                "phone": "",
                "company_name": self.rng.choice(("", "test", "n/a")),
                "company_website": "",
                "signup_source": self.rng.choice(SOURCES),
                "created_at": date(2025, 6, 1).isoformat(),
            }
        )
        self.data.contact_truth.append(
            {"row_id": row_id, "entity_id": f"junk{i:05d}", "defects": "invalid_email"}
        )
        self.defects["invalid_email"] += 1

    def company_rows(self, companies: list[Company]) -> None:
        for company in companies:
            copies = 1 + (self.rng.randint(1, 2) if self.dirty() else 0)
            for copy_index in range(copies):
                defects = ["dup_company"] if copy_index else []
                row_id = f"k{len(self.data.companies) + 1:05d}"
                self.data.companies.append(
                    {
                        "row_id": row_id,
                        "name": self.company_name(company.name, defects),
                        "website": self.website(company.key, defects),
                        "employees": str(company.employees) if self.rng.random() < 0.9 else "",
                        "industry": company.industry,
                    }
                )
                self.data.company_truth.append(
                    {
                        "row_id": row_id,
                        "entity_id": company.key,
                        "defects": ";".join(sorted(set(defects))),
                    }
                )

    def run(self) -> Dataset:
        companies = self.companies()
        # A few companies hold most of the people, as in a real install base.
        weights = [1.0 / (rank + 1) ** 0.8 for rank in range(len(companies))]
        for i in range(self.config.n):
            company = self.rng.choices(companies, weights=weights)[0]
            person = self.person(i, company)
            self.contact_row(person, person.email, person.first, [])
            if self.dirty():
                self.duplicate(person)
                if self.rng.random() < 0.2:
                    self.duplicate(person)
            if self.rng.random() < self.config.dirty_fraction * 0.1:
                self.junk(i)
        self.company_rows(companies)
        return self.data


def generate(config: GenConfig) -> tuple[Dataset, Summary]:
    gen = _Generator(config)
    data = gen.run()
    summary: Summary = {
        "config": asdict(config),
        "contact_rows": len(data.contacts),
        "people": config.n,
        "duplicate_rows": sum(gen.duplicates.values()),
        "duplicates_by_kind": dict(sorted(gen.duplicates.items())),
        "rows_with_defect": dict(sorted(gen.defects.items())),
        "company_rows": len(data.companies),
        "companies": len({t["entity_id"] for t in data.company_truth}),
    }
    return data, summary


def write_dataset(data: Dataset, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "contacts.csv", CONTACT_FIELDS, data.contacts)
    _write_csv(out_dir / "companies.csv", COMPANY_FIELDS, data.companies)
    _write_csv(
        out_dir / "truth_contacts.csv", ("row_id", "entity_id", "defects"), data.contact_truth
    )
    _write_csv(
        out_dir / "truth_companies.csv", ("row_id", "entity_id", "defects"), data.company_truth
    )


def write_summary(summary: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
