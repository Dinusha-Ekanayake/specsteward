"""Project configuration from ``sdd.toml`` at the repository root.

Everything project-specific lives here: where specs are, how a spec is marked approved,
which commands make up the quality gate, which wording is banned, which model reviews
which risk, and the caps that stop runaway loops.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE = "sdd.toml"
RISKS = ("low", "high", "human")
GRAPHIFY_VERSION = "0.9.100"  # pinned; see specsteward/graph.py
SPEC_NUMBER = re.compile(r"\d{3}")
# Files that configure the gate itself. Changing one during a run fails `sdd check` until
# the user looks at it and runs `sdd trust`, so an agent cannot quietly weaken the gate.
GUARD_FILES = (
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    ".coveragerc",
    "mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    "conftest.py",
    "tests/conftest.py",
    "package.json",
    "tsconfig.json",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    "eslint.config.js",
    "eslint.config.mjs",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.ts",
    "vitest.config.js",
    ".prettierrc",
    "go.mod",
    "Cargo.toml",
    "clippy.toml",
    "rustfmt.toml",
    "Makefile",
)


@dataclass(frozen=True)
class CheckStep:
    name: str
    cmd: tuple[str, ...]
    keep: str = ""  # regex for the output lines worth showing; empty keeps the tail
    timeout: int = 900  # seconds; a hanging step fails instead of stalling the run


@dataclass(frozen=True)
class Limits:
    max_review_rounds: int = 2
    max_fix_attempts: int = 3
    max_agent_runs: int = 60
    restart_after_tasks: int = 4
    max_converge_rounds: int = 2  # converge passes that may add tasks before shipping
    max_tokens: int = 0  # fresh tokens per run (input, cache writes, output); 0 = off


@dataclass(frozen=True)
class Wording:
    banned: tuple[str, ...] = ()
    negations: tuple[str, ...] = ("not", "never", "no")
    scan: tuple[str, ...] = ("src",)
    suffixes: tuple[str, ...] = (".py", ".ts", ".js", ".md", ".yaml", ".yml", ".json")
    allow_marker: str = "sdd: allow-wording"


@dataclass(frozen=True)
class Config:
    root: Path
    specs_dir: str = "specs"
    branch_prefix: str = "feature/{spec}-"
    fix_branch_prefix: str = "fix/{spec}-"  # also accepted, for bug-fix specs
    status_regex: str = r"(\*\*Status:\*\*\s*)([^\n]*)"
    approved_word: str = "Approved"
    check: tuple[CheckStep, ...] = ()
    format_cmds: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    wording: Wording = Wording()
    limits: Limits = Limits()
    models: dict[str, str] = field(
        default_factory=lambda: {"low": "sonnet", "high": "opus", "human": "opus"}
    )
    max_group: int = 3
    max_group_high: int = 2
    helper_files: tuple[str, ...] = ()
    path_roots: tuple[str, ...] = ()  # empty: the project's top-level folders
    graphify: bool = True
    guard: tuple[str, ...] = GUARD_FILES
    review_skip: tuple[str, ...] = ()  # risks reviewed only by the final branch review
    graphify_auto: bool = True  # keep a code graph built and fresh, in the background
    graphify_version: str = GRAPHIFY_VERSION
    scope_mode: str = "enforce"  # enforce | warn | off: changes outside the plan's files
    scope_allow: tuple[str, ...] = ()  # extra globs a run may always touch

    def spec_dir(self, spec: str) -> Path:
        if not SPEC_NUMBER.fullmatch(spec):
            raise ValueError(f"spec must be a three-digit number such as 003, got {spec!r}")
        matches = sorted(p for p in (self.root / self.specs_dir).glob(f"{spec}-*") if p.is_dir())
        if not matches:
            raise ValueError(
                f"there is no spec {spec} in {self.specs_dir}/; draft one with "
                "`/specsteward:run plan <topic>`"
            )
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise ValueError(f"spec {spec} is ambiguous ({names}); renumber one of them")
        return matches[0]

    def status_of(self, spec_text: str) -> str:
        match = re.search(self.status_regex, spec_text)
        return match.group(2).strip() if match else ""

    def is_approved(self, spec_text: str) -> bool:
        return self.status_of(spec_text).startswith(self.approved_word)


def _tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value or ())


def _review_skip(raw: dict[str, Any]) -> tuple[str, ...]:
    skip = _tuple(raw.get("skip", ()))
    if set(skip) - {"low"}:
        raise ValueError(
            "[review] skip may only contain 'low'; high and human risk are always reviewed"
        )
    return skip


def _regex(pattern: str, where: str) -> None:
    """Compile at load time, so a bad regex fails `sdd` with a clear message, not mid-run."""
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"{where}: invalid regex {pattern!r}: {error}") from error


def _steps(raw: list[dict[str, Any]]) -> tuple[CheckStep, ...]:
    steps = []
    for item in raw:
        if "name" not in item or "cmd" not in item:
            raise ValueError("each [[check]] step needs a name and a cmd list")
        if isinstance(item["cmd"], str) or not item["cmd"]:
            raise ValueError(f"check step {item['name']!r}: cmd must be a non-empty list")
        timeout = int(item.get("timeout", CheckStep.timeout))
        if timeout < 1:
            raise ValueError(f"check step {item['name']!r}: timeout must be at least 1")
        keep = str(item.get("keep", ""))
        _regex(keep, f"check step {item['name']!r} keep")
        steps.append(CheckStep(str(item["name"]), _tuple(item["cmd"]), keep, timeout))
    return tuple(steps)


def _models(raw: dict[str, Any]) -> dict[str, str]:
    models = {"low": "sonnet", "high": "opus", "human": "opus"} | {
        k: str(v) for k, v in raw.items()
    }
    unknown = set(models) - set(RISKS)
    if unknown:
        raise ValueError(f"[models] keys must be {RISKS}, got {sorted(unknown)}")
    return models


def from_dict(root: Path, raw: dict[str, Any]) -> Config:
    project = raw.get("project", {})
    limits = Limits(**raw.get("limits", {}))
    counts = {k: v for k, v in vars(limits).items() if k != "max_tokens"}
    if min(counts.values()) < 1 or limits.max_tokens < 0:
        raise ValueError("[limits] values must be at least 1 (max_tokens: 0 or more)")
    wording_raw = raw.get("wording", {})
    for pattern in _tuple(wording_raw.get("banned")):
        _regex(pattern, "[wording] banned")
    groups = raw.get("groups", {})
    brief = raw.get("brief", {})
    graph = raw.get("graphify", {})
    scope = raw.get("scope", {})
    scope_mode = str(scope.get("mode", "enforce"))
    if scope_mode not in ("enforce", "warn", "off"):
        raise ValueError("[scope] mode must be 'enforce', 'warn' or 'off'")
    status_regex = str(project.get("status_regex", Config.status_regex))
    _regex(status_regex, "[project] status_regex")
    if re.compile(status_regex).groups < 2:
        raise ValueError(
            "[project] status_regex needs two groups: (prefix)(status text), as the default has"
        )
    return Config(
        root=root,
        specs_dir=str(project.get("specs_dir", "specs")),
        branch_prefix=str(project.get("branch_prefix", "feature/{spec}-")),
        fix_branch_prefix=str(project.get("fix_branch_prefix", "fix/{spec}-")),
        status_regex=status_regex,
        approved_word=str(project.get("approved_word", "Approved")),
        check=_steps(raw.get("check", [])),
        format_cmds={
            suffix: tuple(_tuple(cmd) for cmd in cmds)
            for suffix, cmds in raw.get("format", {}).items()
        },
        wording=Wording(
            banned=_tuple(wording_raw.get("banned")),
            negations=_tuple(wording_raw.get("negations", Wording.negations)),
            scan=_tuple(wording_raw.get("scan", Wording.scan)),
            suffixes=_tuple(wording_raw.get("suffixes", Wording.suffixes)),
            allow_marker=str(wording_raw.get("allow_marker", Wording.allow_marker)),
        ),
        limits=limits,
        models=_models(raw.get("models", {})),
        max_group=int(groups.get("max_size", 3)),
        max_group_high=int(groups.get("max_high", 2)),
        helper_files=_tuple(brief.get("helper_files")),
        path_roots=_tuple(brief.get("path_roots", ())),
        graphify=bool(brief.get("graphify", True)),
        guard=_tuple(project.get("guard", GUARD_FILES)),
        graphify_auto=bool(graph.get("auto", True)),
        graphify_version=str(graph.get("version", GRAPHIFY_VERSION)),
        review_skip=_review_skip(raw.get("review", {})),
        scope_mode=scope_mode,
        scope_allow=_tuple(scope.get("allow")),
    )


def find_root(start: Path) -> Path:
    """The nearest directory at or above ``start`` that has an ``sdd.toml``."""
    for directory in (start, *start.parents):
        if (directory / CONFIG_FILE).exists():
            return directory
    raise ValueError(f"no {CONFIG_FILE} here or above; set the project up with `/specsteward:init`")


def load(start: Path) -> Config:
    root = find_root(start)
    with (root / CONFIG_FILE).open("rb") as handle:
        return from_dict(root, tomllib.load(handle))
