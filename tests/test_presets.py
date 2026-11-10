import json
import sys
from pathlib import Path

import pytest

from specsteward import config
from specsteward.check import project_python, resolve_cmd
from specsteward.init import render_config
from specsteward.presets import detect_stack, preset_for


def _names(root: Path, preset: str = "auto") -> list[str]:
    return [step.name for step in preset_for(root, preset).checks]


@pytest.mark.parametrize(
    ("marker", "stack"),
    [
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("requirements.txt", "python"),
        ("Pipfile", "python"),
        ("script.py", "python"),  # plain scripts, no manifest yet
        ("package.json", "node"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("README.md", "generic"),
    ],
)
def test_detect_stack(tmp_path: Path, marker: str, stack: str) -> None:
    (tmp_path / marker).write_text("{}" if marker == "package.json" else "")
    assert detect_stack(tmp_path) == stack


def test_a_tests_folder_alone_marks_python_but_a_makefile_wins(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("")
    assert detect_stack(tmp_path) == "python"
    assert "tests" in _names(tmp_path)  # so the gate proves something from the start
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    assert detect_stack(tmp_path) == "generic"  # the Makefile is the project's own entry point


def test_python_checks_follow_the_tools_the_project_configures(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\n[tool.mypy]\n[project.optional-dependencies]\ndev = ['pytest-cov']\n"
    )
    assert _names(tmp_path) == ["lint", "format", "types", "tests"]
    tests = preset_for(tmp_path).checks[-1].cmd
    assert tests[0] == "{python}" and "--cov" in tests


def test_python_with_black_and_pyright(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.black]\n[tool.pyright]\n")
    assert _names(tmp_path) == ["format", "types", "tests"]
    assert preset_for(tmp_path).checks[1].cmd == ("pyright",)


def test_python_scan_includes_flat_packages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    assert preset_for(tmp_path).scan == ("mypkg",)


def test_node_checks_come_from_package_json_scripts(tmp_path: Path) -> None:
    package = {
        "scripts": {"lint": "eslint .", "typecheck": "tsc --noEmit", "test": "vitest run"},
        "devDependencies": {"prettier": "3"},
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    chosen = preset_for(tmp_path)
    assert [s.cmd for s in chosen.checks] == [
        ("pnpm", "run", "lint"),
        ("pnpm", "run", "typecheck"),
        ("pnpm", "test"),
    ]
    assert chosen.format_cmds[".ts"] == (("pnpm", "exec", "prettier", "--write", "{file}"),)


def test_node_skips_the_npm_placeholder_test_script(tmp_path: Path) -> None:
    scripts = {"test": 'echo "Error: no test specified" && exit 1'}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    (tmp_path / "tsconfig.json").write_text("{}")
    assert [s.cmd for s in preset_for(tmp_path).checks] == [("npx", "tsc", "--noEmit")]


def test_go_rust_and_make(tmp_path: Path) -> None:
    assert _names(tmp_path, "go") == ["vet", "tests"]
    assert _names(tmp_path, "rust") == ["format", "lint", "tests"]
    (tmp_path / "Makefile").write_text("lint:\n\truff .\ntest:\n\tpytest\n")
    assert _names(tmp_path, "generic") == ["lint", "test"]


def test_empty_generic_project_gets_a_commented_example(tmp_path: Path) -> None:
    text = render_config(tmp_path, "generic")
    assert '# cmd = ["make", "test"]' in text
    (tmp_path / "sdd.toml").write_text(text)
    assert config.load(tmp_path).check == ()


def test_python_placeholder_prefers_the_project_venv(tmp_path: Path) -> None:
    assert project_python(tmp_path) == sys.executable
    venv = tmp_path / ".venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"\x7fELF\x02\x01")  # a native executable, as a real venv holds
    assert project_python(tmp_path) == sys.executable  # no pyvenv.cfg: not a venv
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert project_python(tmp_path) == str(venv)
    assert resolve_cmd(("{python}", "-m", "pytest"), tmp_path)[0] == str(venv)


def test_unknown_preset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preset"):
        preset_for(tmp_path, "cobol")


def test_a_committed_venv_is_never_used(tmp_path: Path) -> None:
    import subprocess

    venv = tmp_path / ".venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text("#!/bin/sh\n")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for args in (["init", "-q"], ["add", "-f", ".venv/bin/python"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    assert project_python(tmp_path) == sys.executable


def test_a_package_json_of_the_wrong_shape_does_not_crash(tmp_path: Path) -> None:
    for body in (
        "[]",
        '"x"',
        '{"scripts": [], "devDependencies": ["a"]}',
        '{"scripts": {"test": 1}}',
    ):
        (tmp_path / "package.json").write_text(body, encoding="utf-8")
        assert preset_for(tmp_path, "node").stack == "node"


def test_bun_runs_the_projects_test_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    (tmp_path / "bun.lockb").write_text("", encoding="utf-8")
    tests = next(step for step in preset_for(tmp_path, "node").checks if step.name == "tests")
    assert tests.cmd == ("bun", "run", "test")


def test_a_script_posing_as_the_venv_python_is_never_used(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text("#!/bin/sh\necho passed\n")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert project_python(tmp_path) == sys.executable
