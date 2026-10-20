from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pipelineos import __version__
from pipelineos.cli import app
from pipelineos.hubspot import FakeHubSpot, HubSpotError
from pipelineos.settings import get_settings

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "portal" / "pipelineos.yaml"


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "HubSpot RevOps build as code" in result.output


def test_validate_ok() -> None:
    result = runner.invoke(app, ["validate", "--spec", str(SPEC)])
    assert result.exit_code == 0
    assert "1 custom objects" in result.stdout


def test_validate_reports_problems(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\n")
    result = runner.invoke(app, ["validate", "--spec", str(bad)])
    assert result.exit_code == 1
    assert "error: version" in result.output


def test_dictionary_write_then_check(tmp_path: Path) -> None:
    out = tmp_path / "dict.md"
    assert runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out)]).exit_code == 0
    ok = runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out), "--check"])
    assert ok.exit_code == 0
    out.write_text(out.read_text() + "drift\n")
    stale = runner.invoke(app, ["dictionary", "--spec", str(SPEC), "--out", str(out), "--check"])
    assert stale.exit_code == 1
    assert "stale" in stale.output


def test_dictionary_bad_spec(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dictionary", "--spec", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1


def test_committed_field_dictionary_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(app, ["dictionary", "--check"], catch_exceptions=False)
    assert result.exit_code == 0, result.output


def test_plan_fake_exits_2_when_changes_pending() -> None:
    result = runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 2
    assert "Plan: 42 to create" in result.stdout
    assert "Manual checklist" in result.stdout


def test_apply_fake_previews_without_yes() -> None:
    result = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 0
    assert "Preview only. Re-run with --yes to apply 42 changes." in result.stdout


def test_apply_fake_yes_converges() -> None:
    result = runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 0, result.output
    assert "Applied 42 of 42 changes." in result.stdout
    assert "Re-plan after apply: 0 changes pending." in result.stdout


def test_converged_portal_has_nothing_to_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = FakeHubSpot()
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: shared)
    assert runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)]).exit_code == 0
    again = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert "Nothing to apply." in again.stdout
    assert runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)]).exit_code == 0


def _real_client(monkeypatch: pytest.MonkeyPatch, portal: int, allowed: str | None) -> None:
    monkeypatch.setenv("PIPELINEOS_HUBSPOT_TOKEN", "tok")
    if allowed is None:
        monkeypatch.delenv("PIPELINEOS_ALLOWED_PORTAL", raising=False)
    else:
        monkeypatch.setenv("PIPELINEOS_ALLOWED_PORTAL", allowed)
    get_settings.cache_clear()
    fake = FakeHubSpot(portal=portal)
    monkeypatch.setattr("pipelineos.cli.HttpHubSpot.from_settings", lambda settings: fake)


def test_apply_refuses_wrong_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=111, allowed="222")
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "token belongs to portal 111" in result.output
    get_settings.cache_clear()


def test_apply_refuses_without_allowed_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=111, allowed=None)
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "PIPELINEOS_ALLOWED_PORTAL" in result.output
    get_settings.cache_clear()


def test_apply_writes_to_allowed_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_client(monkeypatch, portal=333, allowed="333")
    result = runner.invoke(app, ["apply", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 0, result.output
    assert "Re-plan after apply: 0 changes pending." in result.stdout
    get_settings.cache_clear()


def test_real_mode_without_token_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIPELINEOS_HUBSPOT_TOKEN", raising=False)
    monkeypatch.chdir(ROOT / "tests")  # no .env here
    get_settings.cache_clear()
    result = runner.invoke(app, ["plan", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "PIPELINEOS_HUBSPOT_TOKEN is not set (or use --fake)" in result.output
    get_settings.cache_clear()


def test_hubspot_errors_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = FakeHubSpot()
    broken.fail_next = [HubSpotError(503, "UNAVAILABLE", "down")] * 2
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: broken)
    assert runner.invoke(app, ["plan", "--fake", "--spec", str(SPEC)]).exit_code == 1
    result = runner.invoke(app, ["apply", "--fake", "--spec", str(SPEC)])
    assert result.exit_code == 1 and "down" in result.output


def test_apply_reports_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = FakeHubSpot()

    def fail_schema(schema: dict[str, object]) -> dict[str, object]:
        raise HubSpotError(400, "VALIDATION_ERROR", "schema rejected")

    flaky.create_schema = fail_schema  # type: ignore[method-assign]
    monkeypatch.setattr("pipelineos.cli.FakeHubSpot", lambda: flaky)
    result = runner.invoke(app, ["apply", "--fake", "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1
    assert "then failed on schema workspace" in result.stdout


def test_generate_writes_files_and_report(tmp_path: Path) -> None:
    out, report = tmp_path / "raw", tmp_path / "results" / "generated.json"
    result = runner.invoke(
        app, ["generate", "--n", "200", "--seed", "4", "--out", str(out), "--report", str(report)]
    )
    assert result.exit_code == 0, result.output
    assert "contact rows" in result.stdout
    for name in ("contacts.csv", "companies.csv", "truth_contacts.csv", "summary.json"):
        assert (out / name).exists()
    assert report.read_text() == (out / "summary.json").read_text()


def test_clean_command(tmp_path: Path) -> None:
    raw, out, report = tmp_path / "raw", tmp_path / "clean", tmp_path / "cleanup.json"
    assert runner.invoke(app, ["generate", "--n", "300", "--out", str(raw)]).exit_code == 0
    result = runner.invoke(app, ["clean", str(raw), "--out", str(out), "--report", str(report)])
    assert result.exit_code == 0, result.output
    assert "golden" in result.stdout and "vs ground truth: precision" in result.stdout
    assert report.exists() and (out / "report.json").exists()


def test_load_command_preview_write_and_failure(tmp_path: Path) -> None:
    raw, clean_dir = tmp_path / "raw", tmp_path / "clean"
    runner.invoke(app, ["generate", "--n", "200", "--out", str(raw)])
    runner.invoke(app, ["clean", str(raw), "--out", str(clean_dir)])
    base = ["load", str(clean_dir), "--fake", "--spec", str(SPEC)]

    previewed = runner.invoke(app, base)
    assert previewed.exit_code == 0 and "Preview only" in previewed.stdout

    report = tmp_path / "load.json"
    written = runner.invoke(app, [*base, "--yes", "--report", str(report)])
    assert written.exit_code == 0, written.output
    assert "0 failed" in written.stdout and report.exists()

    rows = (clean_dir / "contacts.csv").read_text().splitlines()
    rows[1] = rows[1].replace(rows[1].split(",")[8], "tiktok", 1)  # invalid signup_source
    (clean_dir / "contacts.csv").write_text("\n".join(rows) + "\n")
    failed = runner.invoke(app, [*base, "--yes"])
    assert failed.exit_code == 1
    assert "1 failed" in failed.stdout and "not a valid option" in failed.stdout


def test_load_refuses_wrong_portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw, clean_dir = tmp_path / "raw", tmp_path / "clean"
    runner.invoke(app, ["generate", "--n", "50", "--out", str(raw)])
    runner.invoke(app, ["clean", str(raw), "--out", str(clean_dir)])
    _real_client(monkeypatch, portal=1, allowed="2")
    result = runner.invoke(app, ["load", str(clean_dir), "--yes", "--spec", str(SPEC)])
    assert result.exit_code == 1 and "refusing to write" in result.output
    get_settings.cache_clear()


def test_usage_generate_and_score(tmp_path: Path) -> None:
    raw, clean_dir, usage = tmp_path / "raw", tmp_path / "clean", tmp_path / "usage"
    runner.invoke(app, ["generate", "--n", "400", "--out", str(raw)])
    runner.invoke(app, ["clean", str(raw), "--out", str(clean_dir)])
    made = runner.invoke(app, ["usage", "generate", str(clean_dir), "--out", str(usage)])
    assert made.exit_code == 0, made.output
    assert (usage / "events.csv").exists() and (usage / "truth_archetypes.csv").exists()

    report = tmp_path / "pql.json"
    scored = runner.invoke(
        app,
        [
            "usage",
            "score",
            str(usage / "events.csv"),
            "--clean-dir",
            str(clean_dir),
            "--pql",
            str(ROOT / "portal" / "pql.yaml"),
            "--top",
            "3",
            "--report",
            str(report),
        ],
    )
    assert scored.exit_code == 0, scored.output
    assert "workspaces are product-qualified" in scored.stdout
    assert "PQL rate by workspace type" in scored.stdout
    assert '"by_archetype"' in report.read_text()

    (usage / "truth_archetypes.csv").unlink()
    no_truth = runner.invoke(
        app,
        [
            "usage",
            "score",
            str(usage / "events.csv"),
            "--clean-dir",
            str(tmp_path / "missing"),
            "--pql",
            str(ROOT / "portal" / "pql.yaml"),
        ],
    )
    assert no_truth.exit_code == 0 and "PQL rate by" not in no_truth.stdout


def _usage_fixture(tmp_path: Path) -> tuple[Path, Path]:
    raw, clean_dir, usage = tmp_path / "raw", tmp_path / "clean", tmp_path / "usage"
    runner.invoke(app, ["generate", "--n", "300", "--out", str(raw)])
    runner.invoke(app, ["clean", str(raw), "--out", str(clean_dir)])
    runner.invoke(app, ["usage", "generate", str(clean_dir), "--out", str(usage)])
    return clean_dir, usage / "events.csv"


def test_sync_command_fake(tmp_path: Path) -> None:
    clean_dir, events = _usage_fixture(tmp_path)
    base = [
        "sync",
        str(events),
        "--clean-dir",
        str(clean_dir),
        "--spec",
        str(SPEC),
        "--pql",
        str(ROOT / "portal" / "pql.yaml"),
        "--fake",
    ]
    previewed = runner.invoke(app, base)
    assert previewed.exit_code == 0 and "would send" in previewed.stdout
    report = tmp_path / "sync.json"
    written = runner.invoke(app, [*base, "--yes", "--report", str(report)])
    assert written.exit_code == 0, written.output
    assert "Re-sync with no new usage: 0 records sent." in written.stdout
    assert report.exists()


def test_sync_command_real_mode_saves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipelineos.portal.apply import apply_plan
    from pipelineos.portal.live import read_live
    from pipelineos.portal.plan import build_plan
    from pipelineos.spec import load_spec

    clean_dir, events = _usage_fixture(tmp_path)
    portal = load_spec(SPEC)
    hs = FakeHubSpot(portal=77)
    live = read_live(hs, portal)
    apply_plan(hs, build_plan(portal, live), live)
    monkeypatch.setenv("PIPELINEOS_HUBSPOT_TOKEN", "tok")
    monkeypatch.setenv("PIPELINEOS_ALLOWED_PORTAL", "77")
    get_settings.cache_clear()
    monkeypatch.setattr("pipelineos.cli.HttpHubSpot.from_settings", lambda settings: hs)
    state = tmp_path / "state.json"
    args = [
        "sync",
        str(events),
        "--clean-dir",
        str(clean_dir),
        "--spec",
        str(SPEC),
        "--pql",
        str(ROOT / "portal" / "pql.yaml"),
        "--state",
        str(state),
        "--yes",
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert state.exists()
    second = runner.invoke(app, args)
    assert "sent     0" in second.stdout
    get_settings.cache_clear()


def test_sync_command_unconfigured_portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean_dir, events = _usage_fixture(tmp_path)
    _real_client(monkeypatch, portal=5, allowed="5")
    result = runner.invoke(
        app,
        [
            "sync",
            str(events),
            "--clean-dir",
            str(clean_dir),
            "--spec",
            str(SPEC),
            "--pql",
            str(ROOT / "portal" / "pql.yaml"),
            "--yes",
        ],
    )
    assert result.exit_code == 1 and "run pipelineos apply" in result.output
    get_settings.cache_clear()


def test_classify_command() -> None:
    result = runner.invoke(app, ["classify", "Need SSO, 800 engineers, flaky tests"])
    assert result.exit_code == 0
    assert "ae_enterprise" in result.stdout and "flaky_tests" in result.stdout
    sized = runner.invoke(app, ["classify", "slow builds", "--employees", "120"])
    assert "sdr_mid_market" in sized.stdout


def test_route_demos_command(tmp_path: Path) -> None:
    demos = ROOT / "examples" / "demo_requests.csv"
    base = ["route-demos", str(demos), "--clean-dir", str(tmp_path), "--spec", str(SPEC), "--fake"]
    previewed = runner.invoke(app, base)
    assert previewed.exit_code == 0 and "Preview only" in previewed.stdout
    written = runner.invoke(app, [*base, "--yes"])
    assert written.exit_code == 0, written.output
    assert "Wrote routing for 6 of 6 contacts." in written.stdout


def test_route_demos_reports_failed_rows(tmp_path: Path) -> None:
    demos = tmp_path / "demos.csv"
    demos.write_text(
        "email,company_key,employees,demo_request_text\nnot-an-email,,,pricing?\n"
        "ok@acme.io,,,flaky tests\n"
    )
    result = runner.invoke(
        app,
        [
            "route-demos",
            str(demos),
            "--clean-dir",
            str(tmp_path),
            "--spec",
            str(SPEC),
            "--fake",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "not-an-email" in result.stdout and "Wrote routing for 1 of 2 contacts." in result.stdout


def test_classify_with_ollama_unreachable_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINEOS_OLLAMA_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    result = runner.invoke(app, ["classify", "tests fail randomly", "--ollama"])
    assert result.exit_code == 0 and "keyword_fallback" in result.stdout
    get_settings.cache_clear()
