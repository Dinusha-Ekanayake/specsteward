"""Detect a project's stack and the gate commands it already uses.

``sdd init`` writes an ``sdd.toml`` that works without editing: the checks come from what
the project has (ruff or black, mypy or pyright, package.json scripts, cargo, go, make),
not from a fixed list. ``{python}`` is resolved when the gate runs, so the same file works
on Windows, macOS and Linux, with or without a virtual environment.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STACKS = ("python", "node", "go", "rust", "generic")
PRESETS = ("auto", *STACKS)
_SOURCE_DIRS = ("src", "lib", "app", "pkg", "cmd", "internal")
_NPM_DEFAULT_TEST = "no test specified"


@dataclass(frozen=True)
class Step:
    name: str
    cmd: tuple[str, ...]
    keep: str


@dataclass(frozen=True)
class Preset:
    stack: str
    checks: tuple[Step, ...]
    format_cmds: dict[str, tuple[tuple[str, ...], ...]]
    scan: tuple[str, ...]


PYTHON_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "tox.ini",
)


def _has_python_files(root: Path) -> bool:
    """Plain scripts and a tests/ folder, with no manifest yet: still a Python project."""
    return any(root.glob("*.py")) or any(root.glob("tests/test_*.py"))


def detect_stack(root: Path) -> str:
    if any((root / name).exists() for name in PYTHON_MANIFESTS):
        return "python"
    if (root / "package.json").exists():
        return "node"
    if (root / "go.mod").exists():
        return "go"
    if (root / "Cargo.toml").exists():
        return "rust"
    if not (root / "Makefile").exists() and _has_python_files(root):
        return "python"
    return "generic"


def _read(root: Path, *names: str) -> str:
    return "\n".join(
        (root / name).read_text(encoding="utf-8", errors="replace")
        for name in names
        if (root / name).is_file()
    )


def _python(root: Path) -> Preset:
    text = _read(root, "pyproject.toml", "setup.cfg", "tox.ini", "requirements-dev.txt")
    py = "{python}"
    checks: list[Step] = []
    fmt: list[tuple[str, ...]] = []
    if "[tool.ruff" in text or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        lint = (py, "-m", "ruff", "check", ".", "--output-format", "concise")
        checks.append(Step("lint", lint, r":\d+:\d+:"))
        checks.append(
            Step("format", (py, "-m", "ruff", "format", "--check", "."), "^Would reformat")
        )
        fmt += [(py, "-m", "ruff", "format", "{file}")]
        fmt += [(py, "-m", "ruff", "check", "--fix", "--select", "I", "{file}")]
    elif "[tool.black" in text:
        checks.append(Step("format", (py, "-m", "black", "--check", "."), "^would reformat"))
        fmt.append((py, "-m", "black", "-q", "{file}"))
    if "[tool.mypy" in text or (root / "mypy.ini").exists():
        checks.append(Step("types", (py, "-m", "mypy", "."), "error:"))
    elif "[tool.pyright" in text or (root / "pyrightconfig.json").exists():
        checks.append(Step("types", ("pyright",), "error:"))
    tests = [py, "-m", "pytest", "-q", "--tb=line"]
    if "pytest-cov" in text or "[tool.coverage" in text:
        tests += ["--cov", "--cov-report=term"]
    keep = r"^(FAILED|ERROR)|Required test coverage|\.py:\d+: "
    checks.append(Step("tests", tuple(tests), keep))
    packages = [
        entry.name
        for entry in sorted(root.iterdir())
        if entry.is_dir() and (entry / "__init__.py").exists() and entry.name != "tests"
    ]
    return Preset(
        "python", tuple(checks), {".py": tuple(fmt)} if fmt else {}, _scan(root, packages)
    )


def _package_manager(root: Path) -> tuple[str, tuple[str, ...]]:
    """The runner for scripts and the one for binaries, from the lockfile."""
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm", ("pnpm", "exec")
    if (root / "yarn.lock").exists():
        return "yarn", ("yarn",)
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun", ("bunx",)
    return "npm", ("npx",)


def _node(root: Path) -> Preset:
    try:
        package = json.loads(_read(root, "package.json") or "{}")
    except ValueError:
        package = {}
    package = package if isinstance(package, dict) else {}

    def table(key: str) -> dict[str, Any]:
        value = package.get(key)
        return value if isinstance(value, dict) else {}

    scripts = {k: v for k, v in table("scripts").items() if isinstance(v, str)}
    deps = {**table("dependencies"), **table("devDependencies")}
    pm, exe = _package_manager(root)
    checks: list[Step] = []
    if "lint" in scripts:
        checks.append(Step("lint", (pm, "run", "lint"), r"\d+:\d+\s+error|error"))
    types = next((s for s in ("typecheck", "type-check", "types", "tsc") if s in scripts), None)
    if types:
        checks.append(Step("types", (pm, "run", types), "error TS"))
    elif (root / "tsconfig.json").exists():
        checks.append(Step("types", (*exe, "tsc", "--noEmit"), "error TS"))
    if "test" in scripts and _NPM_DEFAULT_TEST not in scripts["test"]:
        test_cmd = (
            (pm, "run", "test") if pm == "bun" else (pm, "test")
        )  # bun test is its own runner
        checks.append(Step("tests", test_cmd, r"(FAIL|Error:|✕|×)"))
    fmt: dict[str, tuple[tuple[str, ...], ...]] = {}
    if "prettier" in deps or any(root.glob(".prettierrc*")):
        cmd = ((*exe, "prettier", "--write", "{file}"),)
        fmt = {suffix: cmd for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs")}
    return Preset("node", tuple(checks), fmt, _scan(root, []))


def _go(root: Path) -> Preset:
    checks = [Step("vet", ("go", "vet", "./..."), r"\.go:\d+")]
    if any(root.glob(".golangci.y*ml")):
        checks.append(Step("lint", ("golangci-lint", "run"), r"\.go:\d+"))
    checks.append(Step("tests", ("go", "test", "./..."), r"^(--- FAIL|FAIL|panic:)|\.go:\d+"))
    return Preset("go", tuple(checks), {".go": (("gofmt", "-w", "{file}"),)}, _scan(root, []))


def _rust(root: Path) -> Preset:
    checks = (
        Step("format", ("cargo", "fmt", "--check"), r"^Diff in"),
        Step("lint", ("cargo", "clippy", "--all-targets", "--", "-D", "warnings"), r"^(error|-->)"),
        Step("tests", ("cargo", "test", "--quiet"), r"^(test .* FAILED|failures:|panicked)"),
    )
    return Preset("rust", checks, {".rs": (("rustfmt", "{file}"),)}, _scan(root, []))


def _generic(root: Path) -> Preset:
    makefile = _read(root, "Makefile", "makefile")
    targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile, re.MULTILINE))
    checks = tuple(
        Step(target, ("make", target), "")
        for target in ("lint", "typecheck", "test")
        if target in targets
    )
    return Preset("generic", checks, {}, _scan(root, []))


def _scan(root: Path, extra: list[str]) -> tuple[str, ...]:
    found = [name for name in (*_SOURCE_DIRS, *extra) if (root / name).is_dir()]
    return tuple(dict.fromkeys(found)) or ("src",)


BUILDERS = {"python": _python, "node": _node, "go": _go, "rust": _rust, "generic": _generic}


def preset_for(root: Path, preset: str = "auto") -> Preset:
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {PRESETS}")
    stack = detect_stack(root) if preset == "auto" else preset
    return BUILDERS[stack](root)


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def _array(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(_q(item) for item in items) + "]"


def render_checks(preset: Preset) -> str:
    if not preset.checks:
        return (
            "# No lint, type or test commands were found. Add your own, for example:\n"
            '# [[check]]\n# name = "tests"\n# cmd = ["make", "test"]\n'
        )
    blocks = []
    for step in preset.checks:
        keep = f"keep = '{step.keep}'\n" if step.keep and "'" not in step.keep else ""
        blocks.append(f'[[check]]\nname = "{step.name}"\ncmd = {_array(step.cmd)}\n{keep}')
    return "\n".join(blocks)


def render_format(preset: Preset) -> str:
    if not preset.format_cmds:
        return "[format]\n"
    lines = ["[format]"]
    for suffix, cmds in preset.format_cmds.items():
        inner = ",\n".join(f"  {_array(cmd)}" for cmd in cmds)
        lines.append(f"{_q(suffix)} = [\n{inner},\n]")
    return "\n".join(lines) + "\n"
