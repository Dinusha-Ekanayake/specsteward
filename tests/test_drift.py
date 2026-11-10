"""Drift guard: coverage of changed lines, new side effects and function growth."""

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from specsteward import config, drift
from specsteward import state as run_state
from specsteward.config import from_dict
from tests.test_audit_regressions import _symbol_project

COBERTURA = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="src/mod.py"><lines>{lines}</lines></class>
</classes></package></packages></coverage>"""


def _fresh(path: Path) -> None:
    later = time.time() + 5
    os.utime(path, (later, later))  # newer than the edit, as a fresh test run would leave it


def _report(repo: Path, hits: dict[int, int], name: str = "coverage.xml") -> None:
    lines = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in hits.items())
    path = repo / name
    path.write_text(COBERTURA.format(lines=lines), encoding="utf-8")
    _fresh(path)


def _edit(repo: Path, text: str) -> None:
    (repo / "src" / "mod.py").write_text(text, encoding="utf-8")


def test_added_lines_are_the_new_and_changed_ones() -> None:
    assert drift.added_lines("a\nb\nc\n", "a\nB\nc\nd\n") == {2, 4}
    assert drift.added_lines(None, "a\nb\n") == {1, 2}
    assert drift._ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"


def test_a_changed_line_no_test_runs_is_found(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    _edit(repo, "def login():\n    return 1\n\n\ndef other():\n    x = 3\n    return x\n")
    _report(repo, {1: 1, 2: 1, 5: 1, 6: 1, 7: 0})
    assert drift.uncovered(cfg, run) == ["src/mod.py: lines 7 changed but no test runs them"]
    _report(repo, {1: 1, 2: 1, 5: 1, 6: 1, 7: 1})
    assert drift.uncovered(cfg, run) == []


def test_unchanged_uncovered_lines_are_not_blamed(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    _edit(repo, "def login():\n    return 1\n\n\ndef other():\n    return 2\n\n\nX = 1\n")
    _report(repo, {2: 0, 6: 0, 9: 1})
    assert drift.uncovered(cfg, run) == []


def test_a_stale_missing_or_unreadable_report_is_reported(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    _edit(repo, "def login():\n    return 9\n")
    assert "no coverage report" in drift.uncovered(cfg, run)[0]
    (repo / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
    os.utime(repo / "coverage.xml", (1, 1))
    assert "older than the changes" in drift.uncovered(cfg, run)[0]
    (repo / "coverage.xml").write_text("<broken", encoding="utf-8")
    _fresh(repo / "coverage.xml")
    assert "cannot be read" in drift.uncovered(cfg, run)[0]


def test_a_changed_file_missing_from_the_report_is_reported(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    _edit(repo, "def login():\n    return 9\n")
    (repo / "coverage.xml").write_text(
        '<coverage><class filename="other.py"><line number="1" hits="1"/></class></coverage>',
        encoding="utf-8",
    )
    _fresh(repo / "coverage.xml")
    assert "not in the coverage report" in drift.uncovered(cfg, run)[0]


def test_lcov_reports_work_too(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="lcov.info")
    _edit(repo, "def login():\n    return 9\n")
    (repo / "lcov.info").write_text("SF:src/mod.py\nDA:1,1\nDA:2,0\nend_of_record\n")
    _fresh(repo / "lcov.info")
    assert drift.uncovered(cfg, run) == ["src/mod.py: lines 2 changed but no test runs them"]


def test_a_report_outside_the_project_is_refused(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="../outside.xml")
    _edit(repo, "def login():\n    return 9\n")
    assert "outside the project" in drift.uncovered(cfg, run)[0]


def test_new_side_effects_in_a_planned_file_are_found_unless_named(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    _edit(
        repo,
        "import subprocess\n\n\ndef login():\n    return subprocess.run(['x'])\n\n\n"
        "def other():\n    open('f', 'w').write('x')\n    return eval('2')\n",
    )
    assert drift.new_surface(cfg, run) == [
        "src/mod.py: new call eval",
        "src/mod.py: new call open (for writing)",
        "src/mod.py: new call subprocess.run",
        "src/mod.py: new import subprocess",
    ]
    plan = repo / "specs" / "009-demo" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nUses `subprocess`.\n", encoding="utf-8")
    assert "src/mod.py: new import subprocess" not in drift.new_surface(cfg, run)


def test_side_effects_that_were_already_there_are_not_new(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    _edit(repo, "def login():\n    return 1\n\n\ndef other():\n    return open('f').read()\n")
    assert drift.new_surface(cfg, run) == []


def test_javascript_side_effects_are_found() -> None:
    source = "const cp = require('node:child_process');\nimport fs from 'fs';\neval(x);\n"
    assert set(drift.surface("a.js", source)) == {"import child_process", "import fs", "call eval"}


def test_a_function_that_grew_past_the_budget_is_found(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, max_growth=10)
    body = "".join(f"    v{i} = {i} + 1\n" for i in range(8))
    _edit(repo, "def login():\n    return 1\n\n\ndef other():\n" + body + "    return 2\n")
    found = drift.grown(cfg, run)
    assert len(found) == 1 and found[0].startswith("src/mod.py::other grew by ")
    assert drift.grown(replace(cfg, max_growth=0), run) == []
    assert drift.grown(replace(cfg, growth_mode="off"), run) == []


def test_enforce_refuses_and_warn_only_reports(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    _edit(repo, "import socket\n\n\ndef login():\n    return 1\n\n\ndef other():\n    return 2\n")
    assert drift.problem(cfg, run) is None  # warn is the default
    assert any("new side effect" in line for line in drift.notes(cfg, run))
    strict = replace(cfg, surface_mode="enforce")
    assert "new side effect: src/mod.py: new import socket" in str(drift.problem(strict, run))
    assert drift.notes(replace(cfg, surface_mode="off"), run) == []


def test_done_refuses_in_enforce_mode_until_the_drift_is_gone(repo: Path) -> None:
    cfg, _run = _symbol_project(repo)
    cfg = replace(cfg, surface_mode="enforce", symbol_mode="off", check=())
    _edit(repo, "import socket\n\n\ndef login():\n    return 1\n\n\ndef other():\n    return 2\n")
    task = run_state.next_step(cfg)["tasks"][0]
    run_state.record_pass(cfg)
    run_state.record_review(cfg, task, "clean")
    with pytest.raises(ValueError, match="new side effect"):
        run_state.complete(cfg, task)


def test_a_report_edited_after_the_passing_gate_does_not_count(repo: Path) -> None:
    cfg, _run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml", coverage_mode="enforce", check=())
    cfg = replace(cfg, symbol_mode="off")
    _edit(repo, "def login():\n    return 1\n\n\ndef other():\n    return 3\n")
    _report(repo, {1: 1, 2: 1, 5: 1, 6: 0})  # what the gate's test run left behind
    task = run_state.next_step(cfg)["tasks"][0]
    run_state.record_pass(cfg)
    run_state.record_review(cfg, task, "clean")
    with pytest.raises(ValueError, match="lines 6 changed but no test runs them"):
        run_state.complete(cfg, task)
    _report(repo, {1: 1, 2: 1, 5: 1, 6: 1})  # forged by hand: the gate never wrote this
    with pytest.raises(ValueError, match="not the one the last passing gate wrote"):
        run_state.complete(cfg, task)
    run_state.record_pass(cfg)  # the gate ran again and left this report
    run_state.record_review(cfg, task, "clean")
    run_state.complete(cfg, task)


def test_an_imported_name_does_not_hide_the_call() -> None:
    found = drift.surface("a.py", "from os import system\nfrom shutil import rmtree\n")
    assert set(found) == {"call os.system", "call shutil.rmtree"}
    assert not drift.surface("a.py", "from . import os\nimport json\n")
    aliased = "import os as o\nfrom os import system as run\no.system('a')\nrun('b')\n"
    assert drift.surface("a.py", aliased)["call os.system"] == 3  # the import and both calls


def test_naming_a_module_does_not_waive_its_dangerous_calls() -> None:
    assert not drift._named("call os.system", {"os"})
    assert drift._named("call os.system", {"system"})
    assert drift._named("call open (for writing)", {"open"})


def test_the_report_entry_is_the_longest_matching_tail() -> None:
    covered = {"mod.py": {1: 0}, "pkg/mod.py": {1: 1}, "C:/work/app/lib/x.py": {1: 1}}
    assert drift._lookup(covered, "src/pkg/mod.py") == {1: 1}
    assert drift._lookup(covered, "lib/x.py") == {1: 1}
    assert drift._lookup({"a/mod.py": {1: 0}, "b/mod.py": {1: 1}}, "mod.py") is None
    assert drift._lookup(covered, "other.py") is None


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_project_in_a_subfolder_of_the_repository_sees_its_tracked_changes(repo: Path) -> None:
    import shutil

    from specsteward import scope, trust

    project = repo / "apps" / "one"  # the git repository is `repo`; the sdd project is inside
    project.mkdir(parents=True)
    shutil.copytree(repo / "specs", project / "specs")
    shutil.copy(repo / "sdd.toml", project / "sdd.toml")
    (project / "src").mkdir()
    (project / "src" / "mod.py").write_text("def login():\n    return 1\n", encoding="utf-8")
    plan = project / "specs" / "009-demo" / "plan.md"
    plan.write_text("# Plan\n\n| `src/mod.py` | one |\n", encoding="utf-8")
    trust.trust_config(project)
    cfg = config.load(project)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("add", "."),
        ("commit", "-q", "-m", "init"),
    ):
        _git(repo, *args)
    run_state.approve(cfg, "009", "test")
    run_state.start(cfg, "009", check_branch=False)
    run = run_state.require(project)
    (project / "src" / "mod.py").write_text(
        "import socket\n\n\ndef login():\n    return 1\n", encoding="utf-8"
    )
    assert [path for path, _b, _a in scope.changed_sources(cfg, run)] == ["src/mod.py"]
    assert scope.out_of_scope(cfg, run) == []
    assert drift.new_surface(cfg, run) == ["src/mod.py: new import socket"]


def test_a_moved_file_is_compared_with_the_file_it_was(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    _edit(repo, "import socket\n\n\ndef login():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "socket was already here")
    run = {
        **run,
        "scope": {
            **run["scope"],
            "base": __import__("subprocess")
            .run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
            .stdout.strip(),
        },
    }
    _git(repo, "mv", "src/mod.py", "src/moved.py")
    assert drift.new_surface(cfg, run) == []  # the import came with the file, it is not new


def test_a_byte_order_mark_does_not_hide_a_file_from_the_checks(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    (repo / "src" / "mod.py").write_bytes(
        b"\xef\xbb\xbfimport socket\n\n\ndef login():\n    return 1\n"
    )
    assert drift.new_surface(cfg, run) == ["src/mod.py: new import socket"]


def test_lines_are_counted_as_python_counts_them() -> None:
    assert drift.added_lines("x=1\n\x0c\ny=2\n", "x=1\n\x0c\ny=2\nz=3\n") == {4}
    assert drift.added_lines("a\r\nb\r\n", "a\r\nb\r\nc\r\n") == {3}


def test_text_helpers_and_typing_imports_are_not_side_effects() -> None:
    source = (
        "from urllib.parse import quote\nfrom http import HTTPStatus\nimport urllib.parse\n"
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import subprocess\n"
    )
    assert not drift.surface("a.py", source)
    assert set(drift.surface("a.py", "from urllib.request import urlopen\n")) == {"import urllib"}


def test_a_run_with_no_commit_to_compare_against_says_so(repo: Path) -> None:
    cfg, run = _symbol_project(repo)
    run = {**run, "scope": {"base": None, "dirty": {}}}
    assert drift.notes(cfg, run) == [f"not checked: {drift.NO_BASE}"]
    assert drift.problem(cfg, run) is None  # warn mode never blocks
    assert drift.problem(replace(cfg, growth_mode="enforce"), run) == drift.NO_BASE
    quiet = replace(cfg, surface_mode="off", growth_mode="off")
    assert drift.notes(quiet, run) == []  # nothing is switched on, nothing to say


def test_a_changed_file_too_large_to_read_is_reported(repo: Path) -> None:
    from specsteward import scope

    cfg, run = _symbol_project(repo)
    (repo / "src" / "mod.py").write_text("x = 1\n" * 200_000, encoding="utf-8")
    assert list(scope.changed_sources(cfg, run)) == []
    assert drift.notes(cfg, run) == ["not checked: src/mod.py is too large to read"]


def test_the_changed_files_are_read_once_per_report(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from specsteward import scope

    cfg, run = _symbol_project(repo)
    cfg = replace(cfg, coverage_report="coverage.xml")
    _edit(repo, "def login():\n    return 9\n")
    _report(repo, {1: 1, 2: 1})
    calls = []
    real = scope.changed_sources
    monkeypatch.setattr(scope, "changed_sources", lambda c, s: calls.append(1) or list(real(c, s)))
    drift.notes(cfg, run)
    assert len(calls) == 1


def test_a_section_that_is_not_a_table_is_a_clear_error(repo: Path) -> None:
    for bad in ({"coverage": 3}, {"scope": "x"}, {"tdd": []}):
        with pytest.raises(ValueError, match="must be a table"):
            from_dict(repo, bad)


def test_the_settings_are_validated(repo: Path) -> None:
    assert from_dict(repo, {"coverage": {"report": "cov.xml", "mode": "enforce"}}).coverage_mode
    for bad in (
        {"coverage": {"mode": "x"}},
        {"scope": {"surface": "x"}},
        {"scope": {"growth": "x"}},
        {"scope": {"max_growth": -1}},
        {"scope": {"max_growth": "big"}},
    ):
        with pytest.raises(ValueError):
            from_dict(repo, bad)
    cfg = config.load(repo)
    assert (cfg.surface_mode, cfg.growth_mode, cfg.coverage_mode) == ("warn",) * 3
    assert cfg.max_growth == 80 and cfg.coverage_report == ""
