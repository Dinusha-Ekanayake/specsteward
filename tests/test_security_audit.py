"""Regressions for the findings of the 0.6.0 security audit."""

import json
import os
from pathlib import Path

import pytest

from specsteward import config, trust
from specsteward import state as run_state
from specsteward.__main__ import _hook
from specsteward.init import check_specs_dir, init_project, runs_repo_scripts
from specsteward.remote import env_without_tokens
from specsteward.safefs import ensure_safe

# --- a broken sdd.toml must not switch the guards off ---------------------------------------


def test_guard_hooks_keep_guarding_when_sdd_toml_cannot_be_parsed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "sdd.toml").write_text("[[[ not toml", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    payload = {"tool_name": "Bash", "tool_input": {"command": "bash .sdd/bin/sdd trust"}}
    monkeypatch.setenv("SDD_HOOK_PAYLOAD", json.dumps(payload))
    assert _hook("hook-shell-guard") == 0
    out = capsys.readouterr()
    assert "permissionDecision" in out.out and "guarding with the defaults" in out.err


def test_other_hooks_stay_quiet_when_sdd_toml_cannot_be_parsed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "sdd.toml").write_text("[[[ not toml", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert _hook("hook-stop") == 0
    assert capsys.readouterr().out == ""


# --- the review witness switches on once the plugin's hooks are seen --------------------------


def test_clean_verdicts_need_a_reviewer_once_the_hooks_are_live(repo: Path) -> None:
    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    task = run_state.next_step(cfg)["tasks"][0]
    state = run_state.require(repo)
    state["hooks_live"] = True  # set by the bind hook after `sdd start`
    run_state.save(repo, state)
    with pytest.raises(ValueError, match="reviewer"):
        run_state.record_review(cfg, task, "clean")


def test_the_bind_hook_marks_hooks_live(repo: Path) -> None:
    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    payload = {"session_id": "s1", "tool_input": {"command": "bash .sdd/bin/sdd resume"}}
    run_state.bind_owner(cfg, payload)
    assert run_state.require(repo)["hooks_live"] is True


# --- the bot token stays out of child processes -----------------------------------------------


def test_child_environments_never_carry_the_bot_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    env = env_without_tokens()
    assert "TELEGRAM_BOT_TOKEN" not in env and "DISCORD_BOT_TOKEN" not in env
    assert "PATH" in env or "Path" in env


# --- init: the specs dir and what gets trusted -------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        'x"\n[[check]]\nname="p"\ncmd=["curl","evil"]\n#',
        "../../outside",
        "/abs",
        "a/../b",
        "",
        "a b",
    ],
)
def test_specs_dir_must_be_a_plain_relative_folder(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="specs-dir"):
        check_specs_dir(bad)
    with pytest.raises(ValueError, match="specs-dir"):
        init_project(tmp_path, "generic", bad)
    assert not (tmp_path / "sdd.toml").exists()


def test_a_normal_specs_dir_is_accepted() -> None:
    assert check_specs_dir("docs/specs") == "docs/specs"


def test_a_config_that_runs_repository_scripts_is_not_trusted_automatically(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "test": "vitest"}}), encoding="utf-8"
    )
    done = init_project(tmp_path, "node")
    assert any("NOT trusted" in line for line in done)
    assert not trust.config_trusted(tmp_path)
    assert runs_repo_scripts(tmp_path)


def test_a_config_with_fixed_tool_commands_still_starts_trusted(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    assert trust.config_trusted(tmp_path)
    assert not runs_repo_scripts(tmp_path)


# --- paths ---------------------------------------------------------------------------------


def test_ensure_safe_resolves_dot_dot(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        ensure_safe(root, root / ".." / "elsewhere" / "x.md")
    ensure_safe(root, root / "specs" / ".." / "specs" / "x.md")


# --- config regexes --------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["(a+)+$", "(x*)*y", "((ab)+)*"])
def test_nested_quantifier_regexes_are_refused(tmp_path: Path, pattern: str) -> None:
    (tmp_path / "sdd.toml").write_text(
        f"[project]\nstatus_regex = {json.dumps(pattern)}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="regex|status_regex"):
        config.load(tmp_path)


def test_the_default_status_regex_is_allowed() -> None:
    assert config.Config(root=Path(".")).status_of("**Status:** Approved\n") == "Approved"


# --- the gate config baseline catches a quiet downgrade of sdd.toml -----------------------------


def test_swapping_sdd_toml_mid_run_halts_until_the_user_trusts_it(repo: Path) -> None:
    cfg = config.load(repo)
    run_state.start(cfg, "009", check_branch=False)
    original = (repo / "sdd.toml").read_bytes()
    assert run_state.integrity_problem(cfg, run_state.require(repo)) is None
    (repo / "sdd.toml").write_bytes(original + b"\n# quietly weakened\n")
    problem = run_state.integrity_problem(cfg, run_state.require(repo))
    assert problem and "sdd trust" in problem
    run_state.rebaseline_config(repo)  # what `sdd trust` does once the user reviewed it
    assert run_state.integrity_problem(cfg, run_state.require(repo)) is None


def test_env_var_not_leaking_into_os_environ_copy() -> None:
    before = dict(os.environ)
    env_without_tokens()
    assert dict(os.environ) == before
