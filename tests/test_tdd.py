"""Red before green: evidence that a failing test run came before the passing one."""

from dataclasses import replace
from pathlib import Path

import pytest

from specsteward import config
from specsteward import state as run_state
from specsteward.config import CheckStep, from_dict


def _started(repo: Path, mode: str) -> tuple[config.Config, str]:
    cfg = replace(
        config.load(repo),
        tdd_mode=mode,
        check=(CheckStep(name="tests", cmd=("true",)),),
        review_skip=("low",),
    )
    run_state.start(cfg, "009", check_branch=False)
    task = run_state.next_step(cfg)["tasks"][0]
    return cfg, task


def _green(cfg: config.Config) -> None:
    run_state.record_pass(cfg)


def test_a_group_with_no_failing_test_run_cannot_be_ticked_in_enforce_mode(repo: Path) -> None:
    cfg, task = _started(repo, "enforce")
    _green(cfg)
    run_state.record_review(cfg, task, "clean")
    with pytest.raises(ValueError, match="no failing test run"):
        run_state.complete(cfg, task)


def test_a_failing_run_before_the_passing_one_lets_it_through(repo: Path) -> None:
    cfg, task = _started(repo, "enforce")
    run_state.record_red(cfg)
    _green(cfg)
    run_state.record_review(cfg, task, "clean")
    run_state.complete(cfg, task)


def test_red_is_only_recorded_while_a_group_is_in_flight(repo: Path) -> None:
    cfg = replace(config.load(repo), tdd_mode="enforce")
    assert run_state.record_red(cfg) is None  # no run at all
    run_state.start(cfg, "009", check_branch=False)
    assert run_state.record_red(cfg) is None  # a run, but no group yet
    run_state.next_step(cfg)
    state = run_state.record_red(cfg)
    assert state is not None and state["gate"]["red_seen"] is True


def test_a_new_group_starts_without_red() -> None:
    assert run_state._fresh_gate(["T-009-01"])["red_seen"] is False


def test_a_waiver_is_allowed_and_recorded_for_the_user(repo: Path) -> None:
    cfg, task = _started(repo, "enforce")
    _green(cfg)
    run_state.record_review(cfg, task, "clean")
    run_state.complete(cfg, task, waive_red="documentation only")
    notes = run_state.require(repo)["tasks"][task]["assumptions"]
    assert notes == ["red-first waived: documentation only"]


def test_warn_mode_never_blocks_but_says_so(repo: Path) -> None:
    cfg, task = _started(repo, "warn")
    _green(cfg)
    run_state.record_review(cfg, task, "clean")
    note = run_state.red_warning(cfg, run_state.require(repo), task)
    assert note and "no failing test run" in note
    run_state.complete(cfg, task)  # not blocked


def test_off_mode_and_projects_without_a_test_step_are_not_asked(repo: Path) -> None:
    cfg, task = _started(repo, "off")
    assert run_state.red_warning(cfg, run_state.require(repo), task) is None
    no_tests = replace(cfg, tdd_mode="enforce", check=(CheckStep(name="lint", cmd=("true",)),))
    assert run_state.tdd_problem(no_tests, run_state.require(repo), task, "high") is None


def test_human_tasks_have_no_tests_to_run(repo: Path) -> None:
    cfg, task = _started(repo, "enforce")
    assert run_state.tdd_problem(cfg, run_state.require(repo), task, "human") is None


def test_the_mode_is_validated() -> None:
    assert from_dict(Path("."), {"tdd": {"red_first": "enforce"}}).tdd_mode == "enforce"
    assert from_dict(Path("."), {}).tdd_mode == "warn"
    with pytest.raises(ValueError, match="red_first"):
        from_dict(Path("."), {"tdd": {"red_first": "sometimes"}})


def test_a_test_step_is_recognised_by_name() -> None:
    assert run_state.is_test_step("tests") and run_state.is_test_step("Unit Test")
    assert not run_state.is_test_step("lint") and not run_state.is_test_step("types")


def test_a_failing_sdd_check_records_red_and_a_passing_one_does_not(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sys

    from specsteward.__main__ import main

    from .conftest import write_config

    def step(code: int) -> str:
        cmd = json.dumps([sys.executable, "-c", f"raise SystemExit({code})"])
        return f'[[check]]\nname = "tests"\ncmd = {cmd}\n'

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    cfg = write_config(repo, step(0))
    run_state.start(cfg, "009", check_branch=False)
    run_state.next_step(cfg)
    assert main(["check"]) == 0
    assert run_state.require(repo)["gate"]["red_seen"] is False
    write_config(repo, step(1))
    run_state.rebaseline_config(repo)
    assert main(["check"]) == 1
    assert run_state.require(repo)["gate"]["red_seen"] is True
