import json
from pathlib import Path

import pytest

from specsteward import config, trust
from specsteward import state as run_state
from specsteward.tasks import HUMAN, parse_tasks

from .conftest import SPEC, tasks_file, tick, write_config


def _start(cfg: config.Config) -> None:
    run_state.start(cfg, "009", check_branch=False)


def test_start_refuses_unapproved_spec(repo: Path, cfg: config.Config) -> None:
    (repo / "specs" / "009-demo" / "spec.md").write_text(SPEC.replace("Approved", "Draft"))
    with pytest.raises(ValueError, match="not approved"):
        _start(cfg)


def test_start_checks_branch_outside_git(cfg: config.Config) -> None:
    with pytest.raises(ValueError, match="git"):
        run_state.start(cfg, "009")


def test_branch_check_can_be_disabled_in_config(repo: Path) -> None:
    (repo / "sdd.toml").write_text('[project]\nbranch_prefix = ""\n')
    assert run_state.start(config.load(repo), "009")["active"] is True


def test_next_step_reports_group_and_reviewer(cfg: config.Config) -> None:
    _start(cfg)
    step = run_state.next_step(cfg)
    assert step["tasks"] == ["T-009-02", "T-009-03"]
    assert step["reviewer_model"] == "opus"
    assert step["restart_implementer"] is False


def test_same_failure_twice_halts(cfg: config.Config) -> None:
    _start(cfg)
    assert run_state.record_failure(cfg, "T-009-02", "abc")["halted"] is None
    assert "same check failure twice" in run_state.record_failure(cfg, "T-009-02", "abc")["halted"]
    assert run_state.next_step(cfg)["status"] == "halted"


def test_fix_attempt_cap_halts(cfg: config.Config) -> None:
    _start(cfg)
    for sig in ("a", "b", "c"):
        assert run_state.record_failure(cfg, "T-009-02", sig)["halted"] is None
    assert "fix attempt cap" in run_state.record_failure(cfg, "T-009-02", "d")["halted"]


def test_review_round_cap_halts_only_with_open_findings(cfg: config.Config) -> None:
    _start(cfg)
    assert run_state.record_review(cfg, "T-009-02", "changes")["halted"] is None
    assert "review round cap" in run_state.record_review(cfg, "T-009-02", "changes")["halted"]
    run_state.resume(cfg.root)
    assert run_state.record_review(cfg, "T-009-02", "clean")["halted"] is None
    with pytest.raises(ValueError, match="verdict"):
        run_state.record_review(cfg, "T-009-02", "maybe")


def test_agent_run_cap_and_restart_flag(repo: Path) -> None:
    (repo / "sdd.toml").write_text("[limits]\nmax_agent_runs = 1\nrestart_after_tasks = 1\n")
    cfg = config.load(repo)
    _start(cfg)
    tick(cfg, "T-009-02")
    assert run_state.next_step(cfg)["restart_implementer"] is True
    assert run_state.record_agent(cfg, "implementer", fresh=True)["halted"] is None
    assert run_state.next_step(cfg)["restart_implementer"] is False
    assert "agent run cap" in run_state.record_agent(cfg, "reviewer", fresh=False)["halted"]


def test_resume_clears_halt_and_counters(cfg: config.Config) -> None:
    _start(cfg)
    run_state.record_failure(cfg, "T-009-02", "abc")
    run_state.record_failure(cfg, "T-009-02", "abc")
    state = run_state.resume(cfg.root)
    assert state["halted"] is None
    assert state["tasks"]["T-009-02"]["failures"] == []


def test_done_when_all_tasks_closed(repo: Path, cfg: config.Config) -> None:
    _start(cfg)
    for task in parse_tasks(tasks_file(repo)):
        if task.pending:
            tick(cfg, task.id)
    assert run_state.next_step(cfg) == {"status": "done", "spec": "009"}
    assert parse_tasks(tasks_file(repo))[-1].status == HUMAN


def test_stop_and_require(cfg: config.Config) -> None:
    _start(cfg)
    run_state.stop(cfg.root)
    with pytest.raises(ValueError, match="no active"):
        run_state.next_step(cfg)


def test_approve_updates_status_and_logs(repo: Path, cfg: config.Config) -> None:
    spec = repo / "specs" / "009-demo" / "spec.md"
    spec.write_text(SPEC.replace("Approved (2026-11-02)", "Draft"))
    stamp = run_state.approve(cfg, "009", via="telegram")
    assert stamp.startswith("Approved (")
    assert cfg.is_approved(spec.read_text())
    assert "REQ-009-01" in spec.read_text()  # nothing else touched
    entry = json.loads((repo / ".sdd" / "approvals.log").read_text().splitlines()[-1])
    assert entry["spec"] == "009" and entry["via"] == "telegram"


def test_hand_edited_status_line_is_not_an_approval(repo: Path, cfg: config.Config) -> None:
    spec = repo / "specs" / "009-demo" / "spec.md"
    spec.write_text(SPEC.replace("Approved (2026-11-02)", "Draft"))
    run_state.approve(cfg, "009", via="terminal")
    trust.record_approval(repo, "009", "forged-or-stale")  # e.g. a different spec version
    spec.write_text(SPEC)  # an agent writes "Approved" itself
    with pytest.raises(ValueError, match="changed after it was approved"):
        _start(cfg)


def test_status_line_without_any_recorded_approval(repo: Path, cfg: config.Config) -> None:
    store = trust.store_path()
    store.write_text(store.read_text().replace('"009"', '"008"'))
    with pytest.raises(ValueError, match="no recorded approval"):
        _start(cfg)


def test_approval_follows_spec_edits(repo: Path, cfg: config.Config) -> None:
    spec = repo / "specs" / "009-demo" / "spec.md"
    run_state.approve(cfg, "009", via="terminal")
    entry = json.loads((repo / ".sdd" / "approvals.log").read_text().splitlines()[-1])
    assert entry["sha256"] == run_state.approval_digest(spec.parent)
    _start(cfg)
    spec.write_text(spec.read_text() + "- REQ-009-02: slipped in after approval.\n")
    step = run_state.next_step(cfg)
    assert step["status"] == "halted" and "changed after it was approved" in step["reason"]
    run_state.approve(cfg, "009", via="terminal")  # the user re-approves
    run_state.resume(repo)
    assert run_state.next_step(cfg)["status"] == "run"


def test_start_refuses_while_a_run_is_active(cfg: config.Config) -> None:
    _start(cfg)
    run_state.record_failure(cfg, "T-009-02", "abc")
    with pytest.raises(ValueError, match="already active; continue it with `sdd next`"):
        _start(cfg)  # would otherwise wipe the failure counters
    assert run_state.require(cfg.root)["tasks"]["T-009-02"]["fix_attempts"] == 1
    run_state.stop(cfg.root)
    assert _start_state(cfg)["tasks"] == {}  # an explicit (prompted) restart is fine


def _start_state(cfg: config.Config) -> run_state.State:
    return run_state.start(cfg, "009", check_branch=False)


@pytest.mark.parametrize("spec", ["*", "../009", "9", "0091"])
def test_spec_number_is_validated(cfg: config.Config, spec: str) -> None:
    with pytest.raises(ValueError, match="three-digit"):
        cfg.spec_dir(spec)


def test_approve_requires_status_line(repo: Path, cfg: config.Config) -> None:
    (repo / "specs" / "009-demo" / "spec.md").write_text("# no status\n")
    with pytest.raises(ValueError, match="no status line"):
        run_state.approve(cfg, "009", via="terminal")


def test_summary_for_phone(repo: Path, cfg: config.Config) -> None:
    assert run_state.summary(repo) == "No specsteward run in this project."
    _start(cfg)
    run_state.record_assumption(repo, "T-009-02", "right-closed windows")
    tick(cfg, "T-009-06")
    run_state.halt(repo, run_state.require(repo), "need a decision")
    text = run_state.summary(repo)
    assert "Spec 009: 1 done, 1 waiting for you, 4 open. Halted." in text
    assert "Halted: need a decision" in text
    assert "Sign-off needed: T-009-06" in text
    assert "T-009-02: right-closed windows" in text


# --- done needs a passing gate and a clean review for the current tree -------------


def _group_ready(cfg: config.Config) -> list[str]:
    _start(cfg)
    ids: list[str] = run_state.next_step(cfg)["tasks"]
    return ids


def test_done_refuses_without_gate_and_review(cfg: config.Config) -> None:
    first, _ = _group_ready(cfg)
    with pytest.raises(ValueError, match="no passing `sdd check`"):
        run_state.complete(cfg, first)
    run_state.record_pass(cfg)
    with pytest.raises(ValueError, match="no clean review"):
        run_state.complete(cfg, first)
    run_state.record_review(cfg, first, "clean")
    run_state.complete(cfg, first)
    assert parse_tasks(tasks_file(cfg.root))[1].status == "[x]"


def test_ticking_one_task_does_not_invalidate_the_rest_of_the_group(cfg: config.Config) -> None:
    first, second = _group_ready(cfg)
    run_state.record_pass(cfg)
    run_state.record_review(cfg, first, "clean")
    run_state.complete(cfg, first)  # edits tasks.md, which the fingerprint leaves out
    run_state.complete(cfg, second)


def test_code_change_after_the_gate_needs_a_new_gate(repo: Path, cfg: config.Config) -> None:
    first, _ = _group_ready(cfg)
    run_state.record_pass(cfg)
    (repo / "extra_test.py").write_text("def test_adversarial(): ...\n")  # e.g. reviewer's test
    run_state.record_review(cfg, first, "clean")
    with pytest.raises(ValueError, match="no passing `sdd check`"):
        run_state.complete(cfg, first)
    run_state.record_pass(cfg)
    run_state.complete(cfg, first)


def test_changes_verdict_clears_the_review(cfg: config.Config) -> None:
    first, _ = _group_ready(cfg)
    run_state.record_pass(cfg)
    run_state.record_review(cfg, first, "clean")
    run_state.record_review(cfg, first, "changes")
    with pytest.raises(ValueError, match="the run is halted"):  # the review-round cap
        run_state.complete(cfg, first)
    run_state.resume(cfg.root)
    with pytest.raises(ValueError, match="no clean review"):
        run_state.complete(cfg, first)


def test_a_halted_run_takes_no_verdict_agent_run_or_tick(cfg: config.Config) -> None:
    first, _ = _group_ready(cfg)
    run_state.record_pass(cfg)
    run_state.halt(cfg.root, run_state.require(cfg.root), "the user must decide")
    for action in (
        lambda: run_state.record_review(cfg, first, "clean"),
        lambda: run_state.record_agent(cfg, "implementer", False),
        lambda: run_state.complete(cfg, first),
    ):
        with pytest.raises(ValueError, match="halted .the user must decide"):
            action()


def test_done_refuses_tasks_outside_the_group(cfg: config.Config) -> None:
    _group_ready(cfg)
    with pytest.raises(ValueError, match="not in the group in flight"):
        run_state.complete(cfg, "T-009-05")


def test_human_risk_is_always_marked_for_sign_off(cfg: config.Config) -> None:
    _start(cfg)
    tick(cfg, "T-009-06")  # without --human
    assert parse_tasks(tasks_file(cfg.root))[-1].status == HUMAN


def test_failures_default_to_the_group_in_flight(cfg: config.Config) -> None:
    assert run_state.current_task(cfg) is None
    first, _ = _group_ready(cfg)
    assert run_state.current_task(cfg) == first


def test_observed_subagents_count_toward_the_cap(repo: Path) -> None:
    cfg = write_config(repo, "[limits]\nmax_agent_runs = 2\n")
    assert run_state.observe_agent(cfg) is None  # no run
    _start(cfg)
    run_state.observe_agent(cfg)
    run_state.observe_agent(cfg)
    state = run_state.observe_agent(cfg)
    assert state is not None and "agent run cap" in state["halted"]
    assert run_state.observe_agent(cfg) is None  # halted runs are left alone


def test_low_risk_review_can_be_left_to_the_final_review(repo: Path) -> None:
    cfg = write_config(repo, '[review]\nskip = ["low"]\n')
    _start(cfg)
    tick(cfg, "T-009-02")
    tick(cfg, "T-009-03")
    tick(cfg, "T-009-04")
    step = run_state.next_step(cfg)
    assert step["tasks"] == ["T-009-05"] and step["review"] is False
    assert step["reviewer_model"] is None
    run_state.record_pass(cfg)
    run_state.complete(cfg, "T-009-05")  # no review needed for low risk


def test_high_risk_still_needs_review_when_low_is_skipped(repo: Path) -> None:
    cfg = write_config(repo, '[review]\nskip = ["low"]\n')
    _start(cfg)
    assert run_state.next_step(cfg)["review"] is True
    run_state.record_pass(cfg)
    with pytest.raises(ValueError, match="no clean review"):
        run_state.complete(cfg, "T-009-02")


def test_only_low_risk_reviews_can_be_skipped(repo: Path) -> None:
    with pytest.raises(ValueError, match="only contain 'low'"):
        write_config(repo, '[review]\nskip = ["high"]\n')


def test_summary_says_what_comes_next_and_hides_empty_usage(cfg: config.Config) -> None:
    _start(cfg)
    for task in parse_tasks(tasks_file(cfg.root)):
        if task.pending:
            tick(cfg, task.id)
    text = run_state.summary(cfg.root)
    assert "All tasks done; converge, then ship." in text
    assert "Used:" not in text  # nothing measured yet
