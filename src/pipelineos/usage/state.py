"""Sync state: what was last sent for each record, so unchanged records are not re-sent.

HubSpot counts every upsert against the rate limit and writes an audit-log entry even when
nothing changed, so a nightly sync that re-sends 10k identical rows is both slow and noisy. The
state keeps, per object and key, a hash of the properties last sent and the HubSpot record id
(needed to associate a record that did not change this run), plus the association pairs already
created and the date each company first became product-qualified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def content_hash(properties: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(properties, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class SyncState:
    # object -> key -> {"hash": str, "id": str}
    records: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    associations: set[str] = field(default_factory=set)
    pql_since: dict[str, str] = field(default_factory=dict)  # company_key -> ISO date

    def unchanged(self, object_type: str, key: str, properties: dict[str, str]) -> bool:
        seen = self.records.get(object_type, {}).get(key)
        return seen is not None and seen["hash"] == content_hash(properties)

    def record_id(self, object_type: str, key: str) -> str | None:
        seen = self.records.get(object_type, {}).get(key)
        return seen["id"] if seen else None

    def remember(
        self, object_type: str, key: str, properties: dict[str, str], record_id: str
    ) -> None:
        self.records.setdefault(object_type, {})[key] = {
            "hash": content_hash(properties),
            "id": record_id,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "associations": sorted(self.associations),
            "pql_since": dict(sorted(self.pql_since.items())),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SyncState:
        return cls(
            records=data.get("records", {}),
            associations=set(data.get("associations", [])),
            pql_since=data.get("pql_since", {}),
        )


def load_state(path: Path) -> SyncState:
    if not path.exists():
        return SyncState()
    return SyncState.from_json(json.loads(path.read_text()))


def save_state(state: SyncState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), indent=1, sort_keys=True) + "\n")
