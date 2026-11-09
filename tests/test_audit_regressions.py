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
