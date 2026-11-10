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
    assert drift.surface("a.js", source) == {"import child_process", "import fs", "call eval"}


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
