"""Duplicate detection and golden-record merging for contacts and companies.

Matching runs in two passes over a union-find:

1. **Deterministic keys, O(n).** Rows sharing an email key (normalized, ``+tag`` removed, Gmail
   dots removed) are one person. Rows sharing a phone number are one person if their last names
   also match, so a shared office line does not merge a whole company.
2. **Fuzzy, only inside blocks.** Rows are blocked by (company domain, first 4 letters of last
   name) and compared pairwise only within a block, so the cost is O(n * b) for mean block size
   b instead of O(n^2). Within a block, two rows are one person when last names match and first
   names are equal, a known nickname pair, a prefix of one another, or within edit distance,
   unless both rows carry different phone numbers (namesakes at one company).

Each cluster keeps one survivor (most complete row, then earliest, then lowest row id), so the
same input always produces the same output, and every merge is recorded for audit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from pipelineos.data.normalize import (
    clean_company_name,
    company_key,
    company_name_key,
    email_domain,
    email_key,
    is_freemail,
    name_key,
    normalize_email,
    normalize_name,
    normalize_phone,
    registrable_domain,
)

# Common English nickname pairs ("formal short"). Matching is symmetric.
_NICKNAME_PAIRS = """
john jon, john johnny, michael mike, william will, william bill, robert rob, robert bob,
katherine kate, katherine kathy, elizabeth liz, elizabeth beth, christopher chris, daniel dan,
jennifer jen, jennifer jenny, matthew matt, andrew andy, andrew drew, alexander alex,
alexandra alex, samuel sam, samantha sam, benjamin ben, joseph joe, thomas tom, nicholas nick,
jessica jess, rebecca becca, anthony tony, richard rick, richard dick, james jim, james jimmy,
david dave, steven steve, stephen steve, timothy tim, patricia pat, margaret maggie, edward ed,
jonathan jon, nathaniel nate, victoria vicky, susan sue, deborah deb, gregory greg, kenneth ken,
ronald ron, donald don, lawrence larry, jacob jake, zachary zach
"""
NICKNAMES: dict[str, set[str]] = defaultdict(set)
for _pair in _NICKNAME_PAIRS.replace("\n", " ").split(","):
    _formal, _short = _pair.split()
    NICKNAMES[_formal].add(_short)
    NICKNAMES[_short].add(_formal)

FIRST_NAME_MIN_RATIO = 85.0
LAST_NAME_MIN_RATIO = 90.0
BLOCK_PREFIX = 4


@dataclass
class ContactRecord:
    row_id: str
    email: str
    key: str
    first: str
    last: str
    phone: str | None
    company_key: str | None
    company_name: str
    title: str
    source: str
    created_at: str

    @property
    def completeness(self) -> int:
        return sum(bool(v) for v in (self.phone, self.company_key, self.title, self.first))


@dataclass(frozen=True)
class Rejected:
    row_id: str
    reason: str


@dataclass
class MatchStats:
    rows: int = 0
    blocks: int = 0
    largest_block: int = 0
    comparisons: int = 0
    merged_by: Counter[str] = field(default_factory=Counter)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:  # path compression
            self.parent[i], i = root, self.parent[i]
        return root

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


# -- contacts ---------------------------------------------------------------------------------


def prepare_contacts(rows: Iterable[dict[str, str]]) -> tuple[list[ContactRecord], list[Rejected]]:
    records: list[ContactRecord] = []
    rejected: list[Rejected] = []
    for row in rows:
        email = normalize_email(row.get("email", ""))
        if email is None:
            rejected.append(Rejected(row["row_id"], "invalid_email"))
            continue
        records.append(
            ContactRecord(
                row_id=row["row_id"],
                email=email,
                key=email_key(email),
                first=normalize_name(row.get("first_name", "")),
                last=normalize_name(row.get("last_name", "")),
                phone=normalize_phone(row.get("phone", "")),
                company_key=company_key(row.get("company_website", ""), email),
                company_name=clean_company_name(row.get("company_name", "")),
                title=" ".join(row.get("job_title", "").split()),
                source=row.get("signup_source", ""),
                created_at=row.get("created_at", ""),
            )
        )
    return records, rejected


def same_first_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb or kb in NICKNAMES.get(ka, ()):
        return True
    shorter, longer = sorted((ka, kb), key=len)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return True
    return fuzz.ratio(ka, kb) >= FIRST_NAME_MIN_RATIO


def same_last_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka) and (ka == kb or fuzz.ratio(ka, kb) >= LAST_NAME_MIN_RATIO)


def match_contacts(records: list[ContactRecord]) -> tuple[UnionFind, MatchStats]:
    uf = UnionFind(len(records))
    stats = MatchStats(rows=len(records))

    by_key: dict[str, int] = {}
    by_phone: dict[str, list[int]] = defaultdict(list)
    blocks: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, rec in enumerate(records):
        first = by_key.setdefault(rec.key, i)
        if first != i and uf.union(first, i):
            stats.merged_by["email_key"] += 1
        if rec.phone:
            by_phone[rec.phone].append(i)
        if rec.company_key and name_key(rec.last):
            blocks[(rec.company_key, name_key(rec.last)[:BLOCK_PREFIX])].append(i)

    for members in by_phone.values():
        for j in members[1:]:
            stats.comparisons += 1
            if same_last_name(records[members[0]].last, records[j].last) and uf.union(
                members[0], j
            ):
                stats.merged_by["phone"] += 1

    stats.blocks = len(blocks)
    for members in blocks.values():
        stats.largest_block = max(stats.largest_block, len(members))
        for x, i in enumerate(members):
            for j in members[x + 1 :]:
                if uf.find(i) == uf.find(j):
                    continue
                stats.comparisons += 1
                a, b = records[i], records[j]
                if not (same_last_name(a.last, b.last) and same_first_name(a.first, b.first)):
                    continue
                if a.phone and b.phone and a.phone != b.phone:
                    # Same name but two different known numbers: namesakes, not one person.
                    stats.merged_by["vetoed_by_phone"] += 1
                elif uf.union(i, j):
                    stats.merged_by["fuzzy_name"] += 1
    return uf, stats


def _survivor(members: list[ContactRecord]) -> ContactRecord:
    def rank(r: ContactRecord) -> tuple[int, int, str, str]:
        work_email = 0 if is_freemail(email_domain(r.email)) else 1
        return (-work_email, -r.completeness, r.created_at, r.row_id)

    return min(members, key=rank)


def _most_common(values: Iterable[str]) -> str:
    counted = Counter(v for v in values if v)
    if not counted:
        return ""
    best = max(counted.values())
    return min((v for v, c in counted.items() if c == best), key=lambda v: (-len(v), v))


def golden_contact(members: list[ContactRecord]) -> dict[str, str]:
    survivor = _survivor(members)
    earliest = min(members, key=lambda r: (r.created_at, r.row_id))
    return {
        "contact_id": survivor.row_id,
        "email": survivor.email,
        "firstname": _most_common(m.first for m in members),
        "lastname": survivor.last or _most_common(m.last for m in members),
        "phone": survivor.phone or next((m.phone for m in members if m.phone), "") or "",
        "jobtitle": survivor.title or _most_common(m.title for m in members),
        "company_key": survivor.company_key
        or next((m.company_key for m in members if m.company_key), "")
        or "",
        "company_name": _most_common(m.company_name for m in members),
        "signup_source": earliest.source,
        "created_at": earliest.created_at,
        "other_emails": ";".join(sorted({m.email for m in members} - {survivor.email})),
        "merged_rows": ";".join(sorted(m.row_id for m in members)),
    }


@dataclass
class ContactResult:
    golden: list[dict[str, str]]
    rejected: list[Rejected]
    cluster_of: dict[str, str]  # row_id -> survivor contact_id
    stats: MatchStats


def dedupe_contacts(rows: Iterable[dict[str, str]]) -> ContactResult:
    records, rejected = prepare_contacts(rows)
    uf, stats = match_contacts(records)
    clusters: dict[int, list[ContactRecord]] = defaultdict(list)
    for i, rec in enumerate(records):
        clusters[uf.find(i)].append(rec)
    golden: list[dict[str, str]] = []
    cluster_of: dict[str, str] = {}
    for members in clusters.values():
        record = golden_contact(members)
        golden.append(record)
        for m in members:
            cluster_of[m.row_id] = record["contact_id"]
    golden.sort(key=lambda r: r["contact_id"])
    return ContactResult(golden, rejected, cluster_of, stats)


# -- companies --------------------------------------------------------------------------------


@dataclass
class CompanyResult:
    golden: list[dict[str, str]]
    rejected: list[Rejected]
    cluster_of: dict[str, str]  # row_id -> company_key


def dedupe_companies(
    rows: Iterable[dict[str, str]], contacts: list[dict[str, str]]
) -> CompanyResult:
    """Group company rows by registrable domain; rows without a website match by name.

    Companies that only appear on contacts (a work-email domain with no company row) are added
    so every contact's ``company_key`` resolves to a company.
    """
    rows = list(rows)
    name_to_key: dict[str, str] = {}
    keyed: dict[str, list[dict[str, str]]] = defaultdict(list)
    pending: list[dict[str, str]] = []
    for row in rows:
        key = registrable_domain(row.get("website", ""))
        if key is None:
            pending.append(row)
            continue
        keyed[key].append(row)
        name_to_key.setdefault(company_name_key(row.get("name", "")), key)
    for contact in contacts:
        if contact["company_key"] and contact["company_name"]:
            name_to_key.setdefault(
                company_name_key(contact["company_name"]), contact["company_key"]
            )

    rejected: list[Rejected] = []
    for row in pending:
        key = name_to_key.get(company_name_key(row.get("name", "")))
        if key is None:
            rejected.append(Rejected(row["row_id"], "no_domain"))
        else:
            keyed[key].append(row)

    golden: list[dict[str, str]] = []
    cluster_of: dict[str, str] = {}
    for key, members in keyed.items():
        employees = [int(m["employees"]) for m in members if m.get("employees", "").isdigit()]
        golden.append(
            {
                "company_key": key,
                "name": _most_common(clean_company_name(m.get("name", "")) for m in members),
                "domain": key,
                "numberofemployees": str(max(employees)) if employees else "",
                "industry": _most_common(m.get("industry", "") for m in members),
                "merged_rows": ";".join(sorted(m["row_id"] for m in members)),
            }
        )
        for m in members:
            cluster_of[m["row_id"]] = key

    known = set(keyed)
    from_contacts: dict[str, list[str]] = defaultdict(list)
    for contact in contacts:
        if contact["company_key"] and contact["company_key"] not in known:
            from_contacts[contact["company_key"]].append(contact["company_name"])
    for key, names in from_contacts.items():
        golden.append(
            {
                "company_key": key,
                "name": _most_common(names) or key,
                "domain": key,
                "numberofemployees": "",
                "industry": "",
                "merged_rows": "",
            }
        )
    golden.sort(key=lambda r: r["company_key"])
    return CompanyResult(golden, rejected, cluster_of)
