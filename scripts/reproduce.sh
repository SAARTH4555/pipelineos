#!/usr/bin/env bash
# Regenerate every number in results/ and the README's results section.
# Needs only uv. Ollama is optional: without it the committed Ollama eval is kept as-is.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run pipelineos apply --fake --yes --report results/apply_fake.json >/dev/null
uv run pipelineos generate --report results/generated.json
uv run pipelineos clean --report results/cleanup.json
uv run pipelineos load --fake --yes --report results/load_fake.json
uv run pipelineos usage generate
uv run pipelineos usage score --top 0 --report results/pql.json >/dev/null
uv run pipelineos sync --fake --yes --report results/sync_fake.json
uv run python scripts/bench_dedupe.py --n 1000 3000 10000 --out results/bench.json
uv run python scripts/eval_classifier.py --provider keyword --out results/classifier_keyword.json >/dev/null
if curl -sf -m 2 "${PIPELINEOS_OLLAMA_URL:-http://localhost:11434}/api/tags" >/dev/null; then
  uv run python scripts/eval_classifier.py --provider ollama --out results/classifier_ollama.json >/dev/null
else
  echo "Ollama not reachable: keeping the committed results/classifier_ollama.json"
fi
uv run python scripts/render_readme.py
