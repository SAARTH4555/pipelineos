"""Score a demo-form classifier against the hand-labelled sets in evals/.

``dev`` (demo_form.jsonl) was in view while the keyword rules were written, so the keyword score
on it is optimistic. ``holdout`` (demo_form_holdout.jsonl) was written afterwards and never used
to tune anything; quote that one.

Usage:
  uv run python scripts/eval_classifier.py --provider keyword [--out FILE]
  PIPELINEOS_OLLAMA_URL=http://localhost:11434 \\
    uv run python scripts/eval_classifier.py --provider ollama --out results/classifier_ollama.json

Routing accuracy is the number that matters: whether the lead lands in the queue it would have
with the hand labels. Ollama numbers only ever come from a real local run; CI runs the keyword one.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
from collections import Counter
from pathlib import Path

from pipelineos.ai.classify import (
    USE_CASES,
    Classification,
    Classifier,
    KeywordClassifier,
    OllamaClassifier,
)
from pipelineos.ai.route import route

EVALS = Path(__file__).resolve().parents[1] / "evals"
SETS = {"dev": EVALS / "demo_form.jsonl", "holdout": EVALS / "demo_form_holdout.jsonl"}


def evaluate(classifier: Classifier, rows: list[dict[str, object]]) -> dict[str, object]:
    confusion: Counter[tuple[str, str]] = Counter()
    hits = {"use_case": 0, "enterprise_signals": 0, "team_size": 0, "routing": 0}
    sources: Counter[str] = Counter()
    misses: list[dict[str, object]] = []
    started = time.perf_counter()
    for row in rows:
        got = classifier.classify(str(row["text"]))
        sources[got.source] += 1
        gold = Classification(
            row["use_case"],  # type: ignore[arg-type]
            bool(row["enterprise_signals"]),
            row["team_size"],  # type: ignore[arg-type]
            "label",
        )
        confusion[(gold.use_case, got.use_case)] += 1
        hits["use_case"] += got.use_case == gold.use_case
        hits["enterprise_signals"] += got.enterprise_signals == gold.enterprise_signals
        hits["team_size"] += got.team_size == gold.team_size
        same_queue = route(got).queue == route(gold).queue
        hits["routing"] += same_queue
        if not same_queue or got.use_case != gold.use_case:
            misses.append(
                {
                    "id": row["id"],
                    "expected": gold.use_case,
                    "got": got.use_case,
                    "expected_queue": route(gold).queue,
                    "got_queue": route(got).queue,
                }
            )
    elapsed = time.perf_counter() - started
    n = len(rows)
    per_class = {}
    for label in USE_CASES:
        tp = confusion[(label, label)]
        predicted = sum(c for (_, p), c in confusion.items() if p == label)
        actual = sum(c for (g, _), c in confusion.items() if g == label)
        per_class[label] = {
            "precision": round(tp / predicted, 3) if predicted else None,
            "recall": round(tp / actual, 3) if actual else None,
            "support": actual,
        }
    return {
        "examples": n,
        "accuracy": {k: round(v / n, 3) for k, v in hits.items()},
        "per_use_case": per_class,
        "sources": dict(sources),
        "seconds_per_answer": round(elapsed / n, 3),
        "misses": misses,
        "machine": f"{platform.machine()} / Python {platform.python_version()}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("keyword", "ollama"), default="keyword")
    parser.add_argument("--model", default=os.environ.get("PIPELINEOS_OLLAMA_MODEL", "llama3.1:8b"))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    results: dict[str, object] = {}
    for name, path in SETS.items():
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        results[name] = run(args.provider, args.model, rows)
    print(
        json.dumps(
            {
                k: {"accuracy": v["accuracy"], "sources": v["sources"]}  # type: ignore[index]
                for k, v in results.items()
            },
            indent=2,
        )
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")


def run(provider: str, model: str, rows: list[dict[str, object]]) -> dict[str, object]:
    classifier: Classifier
    if provider == "ollama":
        url = os.environ.get("PIPELINEOS_OLLAMA_URL", "http://localhost:11434")
        classifier = OllamaClassifier(url, model)
    else:
        classifier = KeywordClassifier()
    result = evaluate(classifier, rows)
    result["provider"] = provider if provider == "keyword" else f"ollama:{model}"
    if provider == "ollama" and result["sources"] != {"ollama": len(rows)}:
        print("warning: some answers fell back to keyword rules; Ollama may not be running")
    return result


if __name__ == "__main__":
    main()
