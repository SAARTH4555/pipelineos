"""Classify a demo-form answer: what problem it describes, enterprise requirements, team size.

Two implementations behind one Protocol:

* ``KeywordClassifier``: deterministic rules. Always available, used in CI and as the fallback.
* ``OllamaClassifier``: a local model (llama3.1:8b by default) constrained to a JSON schema, so
  the output always parses into the same fields. If Ollama is not running, times out or returns
  something that fails validation, it falls back to the keyword rules and says so in ``source``.

Nothing here talks to HubSpot. ``route.py`` turns a classification into a queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

UseCase = Literal["flaky_tests", "slow_ci", "test_visibility", "compliance", "other"]
USE_CASES: tuple[UseCase, ...] = (
    "flaky_tests",
    "slow_ci",
    "test_visibility",
    "compliance",
    "other",
)


@dataclass(frozen=True)
class Classification:
    use_case: UseCase
    enterprise_signals: bool
    team_size: int | None
    source: str  # "keyword", "ollama" or "keyword_fallback"


class Classifier(Protocol):
    def classify(self, text: str) -> Classification: ...


# -- keyword rules ----------------------------------------------------------------------------

# fmt: off
USE_CASE_TERMS: dict[UseCase, tuple[str, ...]] = {
    "flaky_tests": (
        "flaky", "flakiness", "flakyness", "flake", "intermittent", "randomly", "random test",
        "nondeterministic", "non-deterministic", "pass locally", "retry", "rerun", "race condition",
        "timing issue", "time out intermittently", "unreliable", "then fail", "quarantine",
    ),
    "slow_ci": (
        "slow", "build time", "too long", "queue time", "parallelize", "shard", "test selection",
        "test impact", "caching", "faster feedback", "forever", "critical path", "min per",
        "minutes", "hours",
    ),
    "test_visibility": (
        "dashboard", "visibility", "report", "trend", "which tests", "which service", "owner",
        "test health", "pass rate", "track test", "per team", "per squad", "one place",
    ),
    "compliance": (
        "auditor", "audit trail", "iso 27001", "hipaa", "fda", "regulat", "evidence",
        "retention", "kept for", "change management", "traceab", "compliance", "proof that tests",
    ),
}
# Phrases that mark the main ask; terms right after them count triple.
EMPHASIS = re.compile(
    r"(mostly|main problem|goal:|pain:|is what hurts|#1|use case is)", re.IGNORECASE
)
ENTERPRISE_TERMS = (
    "sso", "saml", "okta", "scim", "audit log", "on-prem", "on prem", "self-hosted",
    "air-gapped", "air gapped", "vpc", "security questionnaire", "security review",
    "security will approve", "vendor risk", "procurement", "msa", "dpa", "data residency",
    "soc 2", "soc2", "iso 27001", "hipaa", "fda", "bank", "regulat", "defense", "role-based",
    "enterprise plan", "auditor",
)
# fmt: on
TEAM_SIZE = re.compile(
    r"(?:team of\s+(?P<a>\d[\d,]*))"
    r"|(?:(?P<b>\d[\d,]*)\s*(?:\+\s*)?(?:[a-z]+\s+)?(?:engineers|developers|devs|people)\b)"
    r"|(?:(?P<c>\d[\d,]*)\s+person\b)",
    re.IGNORECASE,
)


def _has(text: str, term: str) -> bool:
    # whole-word match for short acronyms (so "msa" does not fire inside another word)
    if len(term) <= 4 and term.isalnum():
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def extract_team_size(text: str) -> int | None:
    match = TEAM_SIZE.search(text)
    if match is None:
        return None
    raw = match.group("a") or match.group("b") or match.group("c")
    return int(raw.replace(",", ""))


class KeywordClassifier:
    def classify(self, text: str) -> Classification:
        lowered = text.lower()
        emphasised = " ".join(lowered[m.end() : m.end() + 80] for m in EMPHASIS.finditer(lowered))
        scores: dict[UseCase, int] = {}
        for use_case, terms in USE_CASE_TERMS.items():
            hits = sum(_has(lowered, t) for t in terms)
            boost = sum(_has(emphasised, t) for t in terms) * 2
            scores[use_case] = hits + boost
        best = max(scores.values())
        # Ties go to the order in USE_CASES: flakiness is the most common buying trigger.
        chosen: UseCase = (
            next(u for u in USE_CASES if scores.get(u, 0) == best) if best else "other"
        )
        return Classification(
            use_case=chosen,
            enterprise_signals=any(_has(lowered, t) for t in ENTERPRISE_TERMS),
            team_size=extract_team_size(text),
            source="keyword",
        )


# -- local model ------------------------------------------------------------------------------

SYSTEM_PROMPT = """You label answers to "What are you hoping Quarry will fix?" on a demo form.
Quarry is a CI test-analytics product.

use_case, the main problem the person describes (pick the one they say hurts most):
- flaky_tests: tests that fail randomly or intermittently, reruns, retries, quarantining tests
- slow_ci: long build or pipeline times, queue times, test splitting or selection, CI cost
- test_visibility: dashboards, reports, trends, ownership, knowing which tests fail
- compliance: audits, regulated industries, evidence or retention of test results
- other: no testing problem described (pricing, jobs, partnerships, just exploring)

enterprise_signals: true if they mention SSO/SAML/SCIM, audit logs, on-prem or self-hosted or
air-gapped, security review or questionnaire, procurement, MSA/DPA, data residency, or a regulated
industry (SOC 2, ISO 27001, HIPAA, FDA, banking, defense). Otherwise false.

team_size: the number of engineers/developers/people they state, as an integer, else null.
Answer with JSON only."""

RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "use_case": {"type": "string", "enum": list(USE_CASES)},
        "enterprise_signals": {"type": "boolean"},
        "team_size": {"type": ["integer", "null"]},
    },
    "required": ["use_case", "enterprise_signals", "team_size"],
}


class _ModelAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    use_case: UseCase
    enterprise_signals: bool
    team_size: int | None


class OllamaClassifier:
    def __init__(
        self,
        url: str,
        model: str = "llama3.1:8b",
        *,
        http: httpx.Client | None = None,
        fallback: Classifier | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._http = http or httpx.Client(timeout=timeout_s)
        self._fallback = fallback or KeywordClassifier()

    def classify(self, text: str) -> Classification:
        body = {
            "model": self._model,
            "stream": False,
            "format": RESPONSE_SCHEMA,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        try:
            resp = self._http.post(f"{self._url}/api/chat", json=body)
            resp.raise_for_status()
            answer = _ModelAnswer.model_validate(json.loads(resp.json()["message"]["content"]))
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError):
            result = self._fallback.classify(text)
            return Classification(
                result.use_case, result.enterprise_signals, result.team_size, "keyword_fallback"
            )
        return Classification(
            answer.use_case,
            answer.enterprise_signals,
            grounded_size(answer.team_size, text),
            "ollama",
        )


def grounded_size(size: int | None, text: str) -> int | None:
    """Keep a model-reported team size only if that number is actually written in the text.

    Small models sometimes invent a plausible size for an answer that states none; a size that
    routes a lead to a different team has to come from the prospect, not the model.
    """
    if size is None:
        return None
    digits = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)}
    return size if size in digits else None
