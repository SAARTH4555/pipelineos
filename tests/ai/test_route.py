from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from pipelineos.ai.classify import Classification
from pipelineos.ai.route import route

ROOT = Path(__file__).resolve().parents[2]


def c(
    use_case: str = "flaky_tests", enterprise: bool = False, size: int | None = None
) -> Classification:
    return Classification(use_case, enterprise, size, "keyword")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("classification", "employees", "queue", "segment"),
    [
        (c(size=8), None, "self_serve_nurture", "self_serve"),
        (c(size=49), None, "self_serve_nurture", "self_serve"),
        (c(size=50), None, "sdr_mid_market", "mid_market"),
        (c(size=499), None, "sdr_mid_market", "mid_market"),
        (c(size=500), None, "ae_enterprise", "enterprise"),
        (c(enterprise=True), None, "ae_enterprise", "enterprise"),  # SSO asks, size unknown
        (c(enterprise=True, size=12), None, "sdr_mid_market", "mid_market"),  # small but asks
        (c(enterprise=True, size=60), None, "ae_enterprise", "enterprise"),
        (c(size=5), 1200, "ae_enterprise", "enterprise"),  # CRM size beats the form
        (c("other"), None, "self_serve_nurture", "self_serve"),
        (c("other", size=200), None, "self_serve_nurture", "self_serve"),
        (c("other", size=900), None, "ae_enterprise", "enterprise"),
        (c("other", enterprise=True), None, "ae_enterprise", "enterprise"),
    ],
)
def test_routing_table(
    classification: Classification, employees: int | None, queue: str, segment: str
) -> None:
    routed = route(classification, employees)
    assert (routed.queue, routed.segment) == (queue, segment)


def test_reasons_say_where_size_came_from() -> None:
    assert "1,200 people (CRM)" in route(c(size=5), 1200).reason
    assert "60 people (form)" in route(c(enterprise=True, size=60)).reason
    assert route(c("other")).reason.startswith("No testing problem described")
    assert "asked for enterprise requirements" in route(c(enterprise=True)).reason


def test_eval_script_runs_keyword_on_both_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "eval.json"
    monkeypatch.setattr(sys, "argv", ["eval", "--provider", "keyword", "--out", str(out)])
    runpy.run_path(str(ROOT / "scripts" / "eval_classifier.py"), run_name="__main__")
    text = out.read_text()
    assert '"dev"' in text and '"holdout"' in text and '"per_use_case"' in text
    assert "routing" in capsys.readouterr().out
