from __future__ import annotations

import json

import httpx
import pytest

from pipelineos.ai.classify import (
    RESPONSE_SCHEMA,
    KeywordClassifier,
    OllamaClassifier,
    extract_team_size,
    grounded_size,
)

kw = KeywordClassifier()


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("about 600 developers", 600),
        ("Global team of 4,000 engineers", 4000),
        ("~3000 devs", 3000),
        ("20 iOS devs", 20),
        ("a team of 15", 15),
        ("We are a 5 person startup", 5),
        ("pricing for 25 people?", 25),
        ("40 microservices and 200 repos", None),
        ("pipeline is 45 minutes", None),
    ],
)
def test_team_size_extraction(text: str, size: int | None) -> None:
    assert extract_team_size(text) == size


def test_keyword_use_cases_and_signals() -> None:
    assert kw.classify("our tests fail randomly, we rerun").use_case == "flaky_tests"
    assert kw.classify("builds take 45 minutes").use_case == "slow_ci"
    assert kw.classify("a dashboard of failure trends").use_case == "test_visibility"
    assert kw.classify("SOC 2 auditors want evidence").use_case == "compliance"
    assert kw.classify("pricing?").use_case == "other"
    big = kw.classify("Need SSO via Okta. Slow builds but mostly flaky tests.")
    assert big.use_case == "flaky_tests" and big.enterprise_signals  # emphasis wins
    assert not kw.classify("we are a small team").enterprise_signals
    assert kw.classify("x").source == "keyword"


def test_short_acronyms_match_whole_words_only() -> None:
    assert kw.classify("We need an MSA signed").enterprise_signals
    assert not kw.classify("our Comsat tests").enterprise_signals


def test_grounded_size() -> None:
    assert grounded_size(10, "just looking around") is None
    assert grounded_size(1500, "we have 1,500 engineers") == 1500
    assert grounded_size(None, "40 devs") is None


def _ollama(handler: object) -> OllamaClassifier:
    http = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return OllamaClassifier("http://ollama.test/", "llama3.1:8b", http=http)


def test_ollama_request_and_answer() -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        content = json.dumps(
            {"use_case": "compliance", "enterprise_signals": True, "team_size": 1500}
        )
        return httpx.Response(200, json={"message": {"content": content}})

    result = _ollama(handler).classify("Bank with 1,500 engineers needs audit evidence")
    assert (result.use_case, result.enterprise_signals, result.team_size, result.source) == (
        "compliance",
        True,
        1500,
        "ollama",
    )
    body = seen[0]
    assert body["format"] == RESPONSE_SCHEMA and body["options"] == {"temperature": 0}
    assert body["stream"] is False


def test_ollama_drops_invented_team_size() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        content = json.dumps({"use_case": "other", "enterprise_signals": False, "team_size": 10})
        return httpx.Response(200, json={"message": {"content": content}})

    assert _ollama(handler).classify("just looking").team_size is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"message": {"content": "not json"}}),
        httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {"use_case": "bananas", "enterprise_signals": False, "team_size": None}
                    )
                }
            },
        ),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_ollama_falls_back_to_keywords(response: httpx.Response) -> None:
    result = _ollama(lambda req: response).classify("our tests fail randomly, 12 engineers")
    assert result.source == "keyword_fallback"
    assert result.use_case == "flaky_tests" and result.team_size == 12


def test_ollama_unreachable_falls_back() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    assert _ollama(handler).classify("pricing?").source == "keyword_fallback"
