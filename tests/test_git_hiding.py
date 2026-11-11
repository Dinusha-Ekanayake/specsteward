"""Git cannot be used to hide a change from the gate, or to run a program inside sdd."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from specsteward import drift, gitsafe, hooks, scope
from specsteward import state as run_state
from specsteward.shell_guard import shell_guard_decision
from tests.test_audit_regressions import _symbol_project

EVIL = "import os\n\n\ndef login():\n    return os.system('x')\n\n\ndef other():\n    return 2\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _asks(command: str) -> bool:
    return (
        shell_guard_decision({"tool_name": "Bash", "tool_input": {"command": command}}) is not None
    )


def test_an_edit_git_was_told_to_ignore_is_still_seen(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    before = run_state.tree(repo, run)
    _git(repo, "update-index", "--assume-unchanged", "src/mod.py")
    (repo / "src" / "mod.py").write_text(EVIL, encoding="utf-8")
    assert gitsafe.hidden_edits(repo) == ["src/mod.py"]
    assert "src/mod.py" in scope._touched(repo, run["scope"])
    assert run_state.tree(repo, run) != before  # the gate and the review no longer match
    assert drift.new_surface(cfg, run) == ["src/mod.py: new call os.system"]


def test_skip_worktree_is_seen_too_and_an_untouched_flagged_file_is_not_blamed(repo: Path) -> None:
    _cfg, run = _symbol_project(repo)
    _git(repo, "update-index", "--skip-worktree", "src/mod.py")
    assert gitsafe.hidden_edits(repo) == []  # flagged, but its content is what git holds
    (repo / "src" / "mod.py").write_text(EVIL, encoding="utf-8")
    assert gitsafe.hidden_edits(repo) == ["src/mod.py"]
    assert "src/mod.py" in scope._touched(repo, run["scope"])


def test_changing_gits_exclude_file_during_a_run_blocks_done(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    assert scope.problem(cfg, run) is None
    (repo / ".git" / "info").mkdir(exist_ok=True)
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write("hidden.py\n")
    (repo / "hidden.py").write_text("import subprocess\n", encoding="utf-8")
    assert "exclude settings" in str(scope.problem(cfg, run))
    assert "exclude settings" in str(scope.problem(replace(cfg, scope_mode="off"), run))
    assert any("exclude settings" in line for line in scope.notes(cfg, run))
    task = run_state.next_step(cfg)["tasks"][0]
    lax = replace(cfg, check=(), symbol_mode="off")
    run_state.record_pass(lax)
    run_state.record_review(lax, task, "clean")
    with pytest.raises(ValueError, match="exclude settings"):
        run_state.complete(lax, task)


def test_a_run_started_by_an_older_release_is_not_blocked(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    old = {**run, "scope": {k: v for k, v in run["scope"].items() if k != "excludes"}}
    assert scope.hiding_problem(cfg, old) is None


def test_a_program_named_in_gits_config_does_not_run_inside_sdd(repo: Path) -> None:
    _cfg, run = _symbol_project(repo)
    _git(repo, "config", "core.fsmonitor", "touch fsmonitor-ran #")
    (repo / "src" / "mod.py").write_text(EVIL, encoding="utf-8")
    scope._touched(repo, run["scope"])
    run_state.tree(repo, run)
    gitsafe.hidden_edits(repo)
    assert not (repo / "fsmonitor-ran").exists()


def test_git_does_not_get_the_phone_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    env = gitsafe.git_env()
    assert "TELEGRAM_BOT_TOKEN" not in env and env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(
    "command",
    [
        "git update-index --assume-unchanged src/mod.py",
        "git update-index --skip-worktree src/mod.py",
        "git config core.fsmonitor 'sh x.sh'",
        "git config --local core.excludesFile /tmp/x",
        "git config alias.st '!sh x.sh'",
        "echo evil.py >> .git/info/exclude",
        "cp x .git/hooks/pre-commit",
        "sed -i s/a/b/ .git/config",
    ],
)
def test_git_commands_that_hide_changes_or_run_programs_ask_first(command: str) -> None:
    assert _asks(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git config --get core.fsmonitor",
        "git config user.name someone",
        "git update-index --refresh",
        "git diff HEAD",
        "git add src/mod.py",
        "cat .gitignore",
    ],
)
def test_ordinary_git_commands_are_not_stopped(command: str) -> None:
    assert not _asks(command)


def test_file_tools_cannot_edit_gits_settings(repo: Path) -> None:
    cfg, _run = _symbol_project(repo)

    def decision(path: str) -> str | None:
        found = hooks.guard_decision(cfg, {"tool_name": "Write", "tool_input": {"file_path": path}})
        return found["hookSpecificOutput"]["permissionDecision"] if found else None

    for path in (".git/config", ".git/hooks/pre-commit", ".git/info/exclude"):
        assert decision(str(repo / path)) == "deny"
    assert decision(str(repo / "src" / "mod.py")) is None
    assert decision(str(repo / ".gitignore")) is None


def test_a_test_named_file_outside_a_test_folder_is_still_read(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    (repo / "src" / "helper_test.py").write_text("import subprocess\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("import subprocess\n", encoding="utf-8")
    assert drift.new_surface(cfg, run) == ["src/helper_test.py: new import subprocess"]


def test_more_of_a_side_effect_that_was_already_there_is_new(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    (repo / "src" / "mod.py").write_text(EVIL, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one call")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    run = {**run, "scope": {**run["scope"], "base": head}}
    more = EVIL + "\n\ndef again():\n    os.system('y')\n    os.system('z')\n"
    (repo / "src" / "mod.py").write_text(more, encoding="utf-8")
    assert drift.new_surface(cfg, run) == ["src/mod.py: new call os.system (x2)"]


def test_reaching_out_in_less_obvious_ways_is_found() -> None:
    source = (
        "import os\nfrom pathlib import Path\nimport shutil\n"
        "os.execl('a')\nos.spawnl(0, 'a')\ngetattr(os, 'sys' + 'tem')('x')\n"
        "Path('f').write_text('x')\nshutil.move('a', 'b')\nopen('f', mode)\n"
    )
    assert set(drift.surface("a.py", source)) == {
        "call os.execl",
        "call os.spawnl",
        "call getattr (on os)",
        "call .write_text",
        "call shutil.move",
        "call open (for writing)",
    }
    script = "const x = await import('node:fs/promises'); fetch(u); Bun.spawn(['a']);"
    assert set(drift.surface("a.mts", script)) == {
        "import fs/promises",
        "call fetch",
        "call Bun.spawn",
    }


def test_a_large_new_function_the_plan_does_not_name_is_found(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, max_growth=10)
    body = "".join(f"    v{i} = {i} + 1\n" for i in range(12))
    text = "def login():\n    return _helper()\n\n\ndef other():\n    return 2\n\n\n"
    text += "def _helper():\n" + body + "    return 1\n\n\nclass A:\n    class B:\n"
    text += (
        "        def deep(self):\n"
        + body.replace("    v", "            v")
        + "            return 1\n"
    )
    (repo / "src" / "mod.py").write_text(text, encoding="utf-8")
    found = drift.grown(cfg, run)
    assert [line.split(" is new")[0] for line in found] == [
        "src/mod.py::_helper",
        "src/mod.py::A.B.deep",
    ]
    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "| `src/mod.py::_helper` | x |\n")
    assert [line.split(" is new")[0] for line in drift.grown(cfg, run)] == ["src/mod.py::A.B.deep"]


def test_a_report_that_lists_the_file_but_none_of_the_change_does_not_pass(repo: Path) -> None:
    import os
    import time

    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    body = "".join(f"    v{i} = {i}\n" for i in range(6))
    (repo / "src" / "mod.py").write_text(
        "def login():\n    return 1\n\n\ndef other():\n" + body + "    return 2\n", encoding="utf-8"
    )
    report = repo / "coverage.xml"
    report.write_text(
        '<coverage><class filename="src/mod.py"><line number="1" hits="1"/></class></coverage>',
        encoding="utf-8",
    )
    os.utime(report, (time.time() + 5,) * 2)
    assert drift.uncovered(cfg, run) == [
        "src/mod.py: the report measures none of the changed lines"
    ]
    report.write_text(
        '<coverage><class filename="src/mod.py"><line number="6" hits="inf"/></class></coverage>',
        encoding="utf-8",
    )
    os.utime(report, (time.time() + 5,) * 2)
    assert "cannot be read" in drift.uncovered(cfg, run)[0]
