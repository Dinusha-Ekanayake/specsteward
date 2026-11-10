"""Regressions for the issues a full code review found (release 0.3.4)."""

import json
from pathlib import Path

import pytest

from specsteward import config, trust
from specsteward.check import scan_wording
from specsteward.config import Wording
from specsteward.init import init_project
from specsteward.shell_guard import shell_guard_decision
from specsteward.tasks import parse_tasks
from specsteward.worktree import fingerprint


def _ask(command: str) -> bool:
    decision = shell_guard_decision({"tool_input": {"command": command}})
    return decision is not None


# --- init must not re-trust a config the user deleted -----------------------------------


def test_init_after_deleting_a_tuned_config_does_not_trust_the_default(repo: Path) -> None:
    (repo / "sdd.toml").unlink()
    done = init_project(repo)
    assert any("NOT trusted" in line for line in done)
    assert not trust.config_trusted(repo)


def test_init_in_a_new_project_still_starts_trusted(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    assert trust.config_trusted(tmp_path)


# --- the wording scan --------------------------------------------------------------------


def _wording() -> Wording:
    return Wording(banned=("BANNEDWORD",), scan=("src",), suffixes=(".py",))


def test_wording_scan_ignores_skip_dir_names_in_the_projects_own_path(tmp_path: Path) -> None:
    root = tmp_path / "build" / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 'BANNEDWORD'\n", encoding="utf-8")
    assert not scan_wording(root, _wording()).ok


def test_wording_scan_skips_a_directory_named_like_a_source_file(tmp_path: Path) -> None:
    (tmp_path / "src" / "odd.py").mkdir(parents=True)
    assert scan_wording(tmp_path, _wording()).ok


# --- nested shells -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "bash .sdd/bin/sdd approve 001"',
        "sh -c 'sdd approve 001'",
        'eval "bash .sdd/bin/sdd approve 001"',
        "bash -c 'echo hi; bash .sdd/bin/sdd trust'",
    ],
)
def test_gates_inside_nested_shells_still_ask(command: str) -> None:
    assert _ask(command)


@pytest.mark.parametrize("command", ["bash -c 'echo hello'", "eval ls", "bash script.sh"])
def test_harmless_nested_shells_do_not_ask(command: str) -> None:
    assert not _ask(command)


# --- the fingerprint ---------------------------------------------------------------------


def test_nested_folders_named_like_build_output_are_fingerprinted(repo: Path) -> None:
    nested = repo / "src" / "out"
    nested.mkdir(parents=True)
    (nested / "n.py").write_text("a = 1\n", encoding="utf-8")
    before = fingerprint(repo)
    (nested / "n.py").write_text("a = 2\n", encoding="utf-8")
    assert fingerprint(repo) != before


def test_top_level_build_output_is_not_fingerprinted(repo: Path) -> None:
    out = repo / "dist"
    out.mkdir()
    (out / "bundle.js").write_text("1", encoding="utf-8")
    before = fingerprint(repo)
    (out / "bundle.js").write_text("2", encoding="utf-8")
    assert fingerprint(repo) == before


# --- the trust store ---------------------------------------------------------------------


def test_a_corrupt_trust_store_is_kept_aside_before_it_is_reset(repo: Path) -> None:
    store = trust.store_path()
    store.write_text("{truncated", encoding="utf-8")
    trust.record_run(repo, "run-1")
    assert store.with_name(store.name + ".corrupt").read_text(encoding="utf-8") == "{truncated"
    assert trust.current_run(repo) == "run-1"


# --- tasks, config, brief, state ----------------------------------------------------------


def test_task_header_in_lower_case_and_three_digit_sequence_numbers(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "|id|task|test|risk|status|\n|---|---|---|---|---|\n| T-001-100 | big | t | low | [ ] |\n",
        encoding="utf-8",
    )
    assert [task.id for task in parse_tasks(path)] == ["T-001-100"]


def test_a_task_row_before_the_header_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("| T-001-01 | x | t | low | [ ] |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        parse_tasks(path)


@pytest.mark.parametrize("pattern", ["([", "(only one group)"])
def test_a_bad_status_regex_fails_at_load(tmp_path: Path, pattern: str) -> None:
    (tmp_path / "sdd.toml").write_text(
        f"[project]\nstatus_regex = {json.dumps(pattern)}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="status_regex"):
        config.load(tmp_path)


def test_a_brief_for_an_unknown_task_writes_nothing(repo: Path) -> None:
    from specsteward.brief import write_brief

    cfg = config.load(repo)
    with pytest.raises((ValueError, KeyError)):
        write_brief(cfg, repo / "specs" / "009-demo", ["../../../x/y/z"])
    assert not (repo.parent / "x").exists()


def test_a_malformed_state_file_means_no_run(repo: Path) -> None:
    from specsteward import state

    (repo / ".sdd").mkdir(exist_ok=True)
    for raw in ("[]", "{not json"):
        (repo / ".sdd" / "state.json").write_text(raw, encoding="utf-8")
        assert state.load(repo) is None


# --- the phone allowlist -------------------------------------------------------------------


def test_file_tools_ask_before_editing_the_phone_allowlist(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specsteward.hooks import guard_decision

    state = tmp_path / "telegram-state"
    state.mkdir()
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(state))
    cfg = config.load(repo)

    def decide(path: Path) -> str | None:
        payload = {"cwd": str(repo), "tool_input": {"file_path": str(path)}}
        found = guard_decision(cfg, payload)
        return found["hookSpecificOutput"]["permissionDecision"] if found else None

    assert decide(state / "access.json") == "ask"  # not created yet
    (state / "access.json").write_text("{}", encoding="utf-8")
    assert decide(state / "access.json") == "ask"
    assert decide(state / "other.json") is None


def test_shell_commands_that_write_the_allowlist_ask() -> None:
    assert _ask("echo '{}' > ~/.claude/channels/telegram/access.json")
    assert _ask("sed -i s/a/b/ ~/.claude/channels/discord/access.json")
    assert not _ask("cat ~/.claude/channels/telegram/access.json")


# --- review verdicts need a witnessed reviewer ------------------------------------------------


def test_a_clean_verdict_needs_a_reviewer_the_hook_saw(repo: Path) -> None:
    from specsteward import state as run_state

    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    task = run_state.next_step(cfg)["tasks"][0]
    run_state.record_review(cfg, task, "clean")  # no hook evidence yet: honour-based
    state = run_state.require(repo)
    state["observed_agent_runs"] = 1  # the hook works in this run, but no reviewer finished
    run_state.save(repo, state)
    with pytest.raises(ValueError, match="reviewer"):
        run_state.record_review(cfg, task, "clean")
    run_state.observe_agent(cfg, "specsteward:reviewer", None)
    run_state.record_review(cfg, task, "clean")
    with pytest.raises(ValueError, match="reviewer"):
        run_state.record_review(cfg, task, "clean")  # one reviewer run, one verdict


# --- the run-state lock ----------------------------------------------------------------------


def test_concurrent_state_updates_are_not_lost(repo: Path) -> None:
    import threading

    from specsteward import state as run_state

    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    errors: list[Exception] = []

    def work() -> None:
        try:
            for _ in range(5):
                run_state.observe_agent(cfg, "specsteward:implementer", None)
        except Exception as exc:
            errors.append(exc)

    # threads share the re-entrancy flag's thread-local, so each takes the lock for real
    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = run_state.require(repo)
    assert not errors
    assert state["observed_agent_runs"] == 20


def test_a_stale_state_lock_is_taken_over(repo: Path) -> None:
    import os
    import time

    from specsteward import state as run_state

    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    lock = repo / ".sdd" / "state.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    run_state.observe_agent(cfg, "specsteward:implementer", None)
    assert not lock.exists()


# --- scope: the code stays inside the approved plan -----------------------------------------


def _git_repo(repo: Path) -> None:
    import subprocess

    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("add", "."),
        ("commit", "-q", "-m", "init"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_planned_paths_reads_backticked_files_only() -> None:
    from specsteward.scope import planned_paths

    plan = "| `src/a.py` | x |\n| `pkg/` | y |\nuse `foo` and `--flag` and `src/*.ts`"
    assert planned_paths(plan) == ["src/a.py", "pkg/", "src/*.ts"]


def test_changes_outside_the_plan_block_done_until_reapproved(repo: Path) -> None:
    from specsteward import scope
    from specsteward import state as run_state

    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text("# Plan\n\n| `src/ok.py` | one |\n", encoding="utf-8")
    cfg = config.load(repo)
    _git_repo(repo)
    run_state.approve(cfg, "009", "test")  # the plan changed, so the spec is approved again
    run_state.start(cfg, "009", check_branch=False)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "sneaky.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_ok.py").write_text("def test_a(): ...\n", encoding="utf-8")
    run = run_state.require(repo)
    assert scope.out_of_scope(cfg, run) == ["src/sneaky.py"]
    assert "outside the approved plan" in str(scope.problem(cfg, run))
    task = run_state.next_step(cfg)["tasks"][0]
    run_state.record_pass(cfg)
    run_state.record_review(cfg, task, "clean")
    with pytest.raises(ValueError, match="outside the approved plan"):
        run_state.complete(cfg, task)
    (repo / "src" / "sneaky.py").unlink()
    run_state.record_pass(cfg)
    run_state.record_review(cfg, task, "clean")
    run_state.complete(cfg, task)


def test_scope_can_warn_or_be_off(repo: Path) -> None:
    from specsteward.config import from_dict

    assert from_dict(repo, {"scope": {"mode": "warn"}}).scope_mode == "warn"
    with pytest.raises(ValueError, match="scope"):
        from_dict(repo, {"scope": {"mode": "loose"}})


# --- symbols: growth inside a planned file --------------------------------------------------


def _symbol_project(repo: Path, plan_extra: str = "") -> tuple[config.Config, dict]:
    from specsteward import state as run_state

    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "mod.py").write_text(
        "def login():\n    return 1\n\n\ndef other():\n    return 2\n", encoding="utf-8"
    )
    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text("# Plan\n\n| `src/mod.py` | one |\n" + plan_extra, encoding="utf-8")
    cfg = config.load(repo)
    _git_repo(repo)
    run_state.approve(cfg, "009", "test")
    run_state.start(cfg, "009", check_branch=False)
    return cfg, run_state.require(repo)


def test_unplanned_public_symbols_in_a_planned_file_are_found(repo: Path) -> None:
    from specsteward import scope

    cfg, run = _symbol_project(repo)
    (repo / "src" / "mod.py").write_text(
        "def login():\n    return 1\n\n\ndef other():\n    return 3\n\n\n"
        "def sneaky():\n    return 4\n\n\ndef _private():\n    return 5\n",
        encoding="utf-8",
    )
    assert scope.unplanned_symbols(cfg, run) == ["src/mod.py::other", "src/mod.py::sneaky"]


def test_symbols_the_plan_names_are_allowed(repo: Path) -> None:
    from specsteward import scope

    cfg, run = _symbol_project(repo, "Adds `other()` and `sneaky`.\n")
    (repo / "src" / "mod.py").write_text(
        "def login():\n    return 1\n\n\ndef other():\n    return 3\n\n\n"
        "def sneaky():\n    return 4\n",
        encoding="utf-8",
    )
    assert scope.unplanned_symbols(cfg, run) == []


def test_symbol_mode_enforce_blocks_and_warn_does_not(repo: Path) -> None:
    from dataclasses import replace

    from specsteward import scope

    cfg, run = _symbol_project(repo)
    (repo / "src" / "mod.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    assert scope.problem(cfg, run) is None  # warn is the default
    strict = replace(cfg, symbol_mode="enforce")
    assert "unplanned changes inside planned files" in str(scope.problem(strict, run))
    assert scope.unplanned_symbols(replace(cfg, symbol_mode="off"), run) == []


def test_javascript_exports_and_bad_symbol_mode() -> None:
    from specsteward.config import from_dict
    from specsteward.scope import _symbols

    assert set(_symbols("a.ts", "export function a() {}\nexport const b = 1\nfunction c() {}")) == {
        "a",
        "b",
    }
    with pytest.raises(ValueError, match="symbols"):
        from_dict(Path("."), {"scope": {"symbols": "x"}})


# --- scope hardening ------------------------------------------------------------------------


def test_a_file_dirty_before_the_run_is_still_watched_for_new_changes(repo: Path) -> None:
    from specsteward import scope
    from specsteward import state as run_state

    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "old.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text("# Plan\n\n| `src/new.py` | one |\n", encoding="utf-8")
    cfg = config.load(repo)
    _git_repo(repo)
    (repo / "src" / "old.py").write_text("def a():\n    return 2\n", encoding="utf-8")  # dirty
    run_state.approve(cfg, "009", "test")
    run_state.start(cfg, "009", check_branch=False)
    run = run_state.require(repo)
    assert scope.out_of_scope(cfg, run) == []  # the earlier edit is not the run's
    (repo / "src" / "old.py").write_text("def a():\n    return 3\n", encoding="utf-8")
    assert scope.out_of_scope(cfg, run) == ["src/old.py"]


def test_a_passing_mention_does_not_name_a_symbol() -> None:
    from specsteward.scope import named_symbols

    plan = (
        "# Plan\n\n## Modules / files\n| `src/a.py` | `login` |\n\n## Risks\nNever touch `purge`.\n"
    )
    tasks = "| T-009-01 | add `refresh` | t | low | [ ] |\n"
    assert named_symbols(plan, tasks) == {"src", "a", "py", "login", "refresh"}


def test_a_clean_verdict_needs_a_reviewer_after_the_last_passing_gate(repo: Path) -> None:
    from specsteward import state as run_state

    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    task = run_state.next_step(cfg)["tasks"][0]
    state = run_state.require(repo)
    state["observed_agent_runs"] = 1
    run_state.save(repo, state)
    run_state.observe_agent(cfg, "specsteward:reviewer", None)
    run_state.record_pass(cfg)  # the code passed the gate again after that reviewer
    with pytest.raises(ValueError, match="reviewer"):
        run_state.record_review(cfg, task, "clean")
    run_state.observe_agent(cfg, "specsteward:reviewer", None)
    run_state.record_review(cfg, task, "clean")
