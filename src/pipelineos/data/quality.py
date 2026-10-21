"""Data-quality scoring and duplicate-detection accuracy.

Two different questions, kept apart on purpose:

* ``quality_score`` is what a RevOps team can measure on any CRM table without knowing the truth:
  valid emails, exact-key uniqueness, standardized formats and resolvable company. It is computed
  the same way before and after cleanup.
* ``evaluate_*`` compares the cleanup's clusters with the generator's ground truth: pairwise
  precision and recall, recall per duplicate kind, and how well junk rows were caught. Only this
  module reads ``truth_*.csv``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from pipelineos.data.normalize import (
    company_key,
    email_key,
    normalize_email,
    normalize_name,
    normalize_phone,
)


def _pairs(n: int) -> int:
    return n * (n - 1) // 2


def quality_score(rows: list[dict[str, str]], columns: Mapping[str, str]) -> dict[str, float]:
    """Score a contact table 0-100. ``columns`` maps email/first/last/phone/website/company_key
    to the table's column names (raw imports and golden records name them differently)."""
    if not rows:
        return {
            "validity": 0.0,
            "uniqueness": 0.0,
            "standardization": 0.0,
            "completeness": 0.0,
            "score": 0.0,
        }
    n = len(rows)
    emails = [normalize_email(r.get(columns["email"], "")) for r in rows]
    keys = Counter(email_key(e) for e in emails if e)
    valid = sum(e is not None for e in emails)
    unique = sum(1 for e in emails if e and keys[email_key(e)] == 1)

    def standard(r: dict[str, str]) -> bool:
        email = r.get(columns["email"], "")
        phone = r.get(columns["phone"], "")
        first, last = r.get(columns["first"], ""), r.get(columns["last"], "")
        return (
            email == email.strip().lower()
            and first == normalize_name(first)
            and last == normalize_name(last)
            and (not phone or normalize_phone(phone) == phone)
        )

    def resolvable(r: dict[str, str], email: str | None) -> bool:
        if "company_key" in columns:
            return bool(r.get(columns["company_key"]))
        return company_key(r.get(columns["website"], ""), email) is not None

    parts = {
        "validity": valid / n,
        "uniqueness": unique / n,
        "standardization": sum(standard(r) for r in rows) / n,
        "completeness": sum(resolvable(r, e) for r, e in zip(rows, emails, strict=True)) / n,
    }
    parts = {k: round(v, 4) for k, v in parts.items()}
    parts["score"] = round(100 * sum(parts.values()) / len(parts), 1)
    return parts


RAW_COLUMNS = {
    "email": "email",
    "first": "first_name",
    "last": "last_name",
    "phone": "phone",
    "website": "company_website",
}
GOLDEN_COLUMNS = {
    "email": "email",
    "first": "firstname",
    "last": "lastname",
    "phone": "phone",
    "company_key": "company_key",
}


def pairwise(predicted: Mapping[str, str], truth: Mapping[str, str]) -> dict[str, float]:
    """Pair-counting precision/recall of a clustering against true entities, in O(n)."""
    rows = [r for r in predicted if r in truth]
    joint = Counter((predicted[r], truth[r]) for r in rows)
    pred_sizes = Counter(predicted[r] for r in rows)
    true_sizes = Counter(truth[r] for r in rows)
    tp = sum(_pairs(c) for c in joint.values())
    pred_pairs = sum(_pairs(c) for c in pred_sizes.values())
    true_pairs = sum(_pairs(c) for c in true_sizes.values())
    precision = tp / pred_pairs if pred_pairs else 1.0
    recall = tp / true_pairs if true_pairs else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_pairs": true_pairs,
        "predicted_pairs": pred_pairs,
    }


def evaluate_contacts(
    cluster_of: Mapping[str, str],
    rejected: Iterable[str],
    truth_rows: list[dict[str, str]],
) -> dict[str, object]:
    truth = {t["row_id"]: t["entity_id"] for t in truth_rows}
    junk = {r for r, e in truth.items() if e.startswith("junk")}
    rejected_set = set(rejected)
    real_truth = {r: e for r, e in truth.items() if r not in junk}

    # Per duplicate kind: was the duplicate row put in the same cluster as its entity's first row?
    first_row: dict[str, str] = {}
    for t in truth_rows:
        if t["row_id"] not in junk:
            first_row.setdefault(t["entity_id"], t["row_id"])
    caught: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for t in truth_rows:
        for defect in t["defects"].split(";"):
            if not defect.startswith("dup_"):
                continue
            kind = defect.removeprefix("dup_")
            total[kind] += 1
            original = first_row[t["entity_id"]]
            if (
                t["row_id"] in cluster_of
                and original in cluster_of
                and cluster_of[t["row_id"]] == cluster_of[original]
            ):
                caught[kind] += 1

    residual: dict[str, set[str]] = defaultdict(set)
    for row_id, cluster in cluster_of.items():
        if row_id in real_truth:
            residual[real_truth[row_id]].add(cluster)
    golden_rows = len(set(cluster_of.values()))
    extra = sum(len(c) - 1 for c in residual.values())

    valid_rows = len(real_truth)
    return {
        "pairwise": pairwise(cluster_of, real_truth),
        "recall_by_kind": {k: round(caught[k] / total[k], 4) for k in sorted(total)},
        "duplicates_by_kind": dict(sorted(total.items())),
        "junk": {
            "rows": len(junk),
            "rejected": len(junk & rejected_set),
            "valid_rows_wrongly_rejected": len(rejected_set - junk),
        },
        "duplicate_rate_before": round((valid_rows - len(residual)) / valid_rows, 4)
        if valid_rows
        else 0.0,
        "duplicate_rate_after": round(extra / golden_rows, 4) if golden_rows else 0.0,
    }


def evaluate_companies(
    cluster_of: Mapping[str, str], truth_rows: list[dict[str, str]]
) -> dict[str, object]:
    truth = {t["row_id"]: t["entity_id"] for t in truth_rows}
    return {
        "pairwise": pairwise(cluster_of, truth),
        "rows_unresolved": len(set(truth) - set(cluster_of)),
        "key_accuracy": round(
            sum(cluster_of[r] == truth[r] for r in cluster_of if r in truth) / len(cluster_of), 4
        )
        if cluster_of
        else 0.0,
    }
