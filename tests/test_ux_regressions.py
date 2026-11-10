"""Messages and orderings a user (or the orchestrating model) depends on."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from specsteward import trust
from specsteward.__main__ import main
from specsteward.analyze import analyze
from specsteward.state import approval_digest
from tests.test_audit_regressions import _symbol_project


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_approve_and_start_on_the_wrong_branch_approves_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com")):
        _git(repo, *args)
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nedited after approval\n")
    assert main(["approve", "009", "--start"]) == 2
    err = capsys.readouterr()
    assert "branch first; nothing was approved" in err.err
    assert "Approved" not in err.out
    current = approval_digest(repo / "specs" / "009-demo")
    assert trust.approved_hash(repo, "009") != current  # the stale approval was left as it was
    _git(repo, "checkout", "-q", "-b", "feature/009-demo")
    assert main(["approve", "009", "--start"]) == 0


def test_a_plan_that_names_no_files_is_a_finding(repo: Path) -> None:
    spec = repo / "specs" / "009-demo"
    assert not [f for f in analyze(spec) if "names no files" in f.message]
    (spec / "plan.md").write_text("# Plan\n\nWe will build the thing.\n", encoding="utf-8")
    found = [f for f in analyze(spec) if "names no files" in f.message]
    assert [(f.level, f.where) for f in found] == [("HIGH", "plan.md")]


def test_scope_in_warn_mode_says_so_when_a_task_is_ticked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from specsteward import config
    from specsteward import state as run_state
    from tests.conftest import write_config

    cfg, _run = _symbol_project(repo)
    text = (repo / "sdd.toml").read_text(encoding="utf-8")
    write_config(repo, text + '\n[scope]\nmode = "warn"\n[review]\nskip = ["low"]\n')
    run_state.rebaseline_config(repo)
    cfg = replace(config.load(repo), check=())
    (repo / "src" / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
    task = run_state.next_step(cfg)["tasks"][0]
    run_state.record_pass(cfg)
    run_state.record_review(cfg, task, "clean")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    capsys.readouterr()
    assert main(["done", task]) == 0
    assert "warning: outside the plan: src/sneaky.py" in capsys.readouterr().err


def test_a_wrong_kind_of_value_is_a_message_not_a_traceback(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    text = (repo / "sdd.toml").read_text(encoding="utf-8")
    (repo / "sdd.toml").write_text(text + "\n[limits]\nmax_foo = 3\n", encoding="utf-8")
    assert main(["status"]) == 2
    assert "[limits] has no key 'max_foo'" in capsys.readouterr().err


def test_every_command_explains_itself(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("init", "start", "approve", "done", "review", "scope", "doctor", "halt"):
        with pytest.raises(SystemExit):
            main([command, "--help"])
        shown = capsys.readouterr().out
        assert len(shown.splitlines()) > 3, command  # a description, not only the usage line
    with pytest.raises(SystemExit):
        main(["start", "--help"])
    assert "three-digit spec number" in capsys.readouterr().out
