"""Drift guard: what a run does *inside* the files its plan already names.

The scope guard (scope.py) stops a run from wandering into files and public symbols the plan
does not name. It cannot see how much behaviour changes inside a function the plan does name.
Three cheap, deterministic checks narrow that gap (none of them proves the code correct):

* changed-line coverage: every line the run added or changed must be run by a test, read from
  the coverage report the project's own test step writes (Cobertura XML or lcov);
* new side-effect surface: new imports of, or calls to, processes, the network, the
  filesystem, `eval` and the like, unless the plan or the tasks name them;
* a growth budget: a function that grew by more than ``max_growth`` syntax-tree nodes.
"""

import ast
import difflib
import hashlib
import re
import xml.etree.ElementTree as ET  # nosec B405 - reads the project's own coverage report
from pathlib import Path
from typing import Any

from specsteward import scope
from specsteward.config import Config
from specsteward.safefs import ensure_safe

MAX_REPORT_BYTES = 20_000_000
MAX_DIFF_LINES = 20_000  # beyond this every line counts as changed; difflib is quadratic
CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs")

PY_MODULES = frozenset(
    ("subprocess", "socket", "urllib", "http", "requests", "httpx", "aiohttp", "ctypes")
    + ("pickle", "marshal", "shelve", "ftplib", "smtplib", "telnetlib", "paramiko")
    + ("multiprocessing", "webbrowser", "importlib")
)
PY_CALLS = frozenset(
    ("os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.removedirs")
    + ("os.execv", "os.execvp", "os.kill", "shutil.rmtree", "eval", "exec", "__import__")
)
# Parts of a watched package that only handle text or constants.
PY_SAFE = frozenset(("urllib.parse", "urllib.error", "http.cookies", "http.HTTPStatus"))
PY_SAFE |= frozenset(("http.HTTPMethod", "importlib.metadata", "importlib.resources"))
JS_MODULES = "child_process|fs|net|http|https|dgram|vm|worker_threads|cluster"
JS_REQUIRE = re.compile(rf"""require\(\s*['"](?:node:)?({JS_MODULES})['"]\s*\)""")
JS_IMPORT = re.compile(rf"""from\s+['"](?:node:)?({JS_MODULES})['"]""")
JS_CALLS = re.compile(r"\b(eval)\s*\(|\bnew\s+(Function)\s*\(")


# --- changed lines -------------------------------------------------------------------------


def added_lines(before: str | None, after: str) -> set[int]:
    """1-based numbers of the lines in ``after`` that are new or different."""
    new = _lines(after)
    old = _lines(before) if before is not None else []
    if before is None or len(old) + len(new) > MAX_DIFF_LINES:
        return set(range(1, len(new) + 1))
    found: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            found.update(range(j1 + 1, j2 + 1))
    return found


def _lines(text: str) -> list[str]:
    """Lines as Python and coverage tools count them (`str.splitlines` also breaks on form
    feeds and U+2028, which would shift every later number)."""
    lines = re.split(r"\r\n|\r|\n", text)
    return lines[:-1] if lines and lines[-1] == "" else lines


def _ranges(numbers: list[int]) -> str:
    spans: list[str] = []
    start = prev = numbers[0]
    for number in (*numbers[1:], None):
        if number is not None and number == prev + 1:
            prev = number
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        if number is not None:
            start = prev = number
    return ", ".join(spans)


# --- coverage ------------------------------------------------------------------------------


def _cobertura(text: str) -> dict[str, dict[int, int]]:
    covered: dict[str, dict[int, int]] = {}
    root = ET.fromstring(text)  # nosec B314 - size-capped, from the project's own test run
    for cls in root.iter("class"):
        name = str(cls.get("filename", "")).replace("\\", "/")
        lines = covered.setdefault(name, {})
        for line in cls.iter("line"):
            number, hits = int(line.get("number", 0)), int(float(line.get("hits", 0)))
            lines[number] = max(lines.get(number, 0), hits)
    return covered


def _lcov(text: str) -> dict[str, dict[int, int]]:
    covered: dict[str, dict[int, int]] = {}
    current: dict[int, int] | None = None
    for raw in text.splitlines():
        if raw.startswith("SF:"):
            current = covered.setdefault(raw[3:].strip().replace("\\", "/"), {})
        elif raw.startswith("DA:") and current is not None:
            number, _, rest = raw[3:].partition(",")
            hits = rest.split(",")[0]
            current[int(number)] = max(current.get(int(number), 0), int(float(hits)))
    return covered


def read_coverage(path: Path) -> dict[str, dict[int, int]]:
    """Lines and hit counts per file from a Cobertura XML or an lcov report."""
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("coverage report is too large")
    text = path.read_text(encoding="utf-8", errors="replace")
    return _cobertura(text) if text.lstrip().startswith("<") else _lcov(text)


def _lookup(covered: dict[str, dict[int, int]], path: str) -> dict[int, int] | None:
    """The report's entry for ``path``. Reports name files relative to their own source
    root (`mod.py` for `src/mod.py`) or absolutely, so the longest matching tail wins and a
    name that fits two changed files the same way is not guessed at."""
    if path in covered:
        return covered[path]
    best: list[tuple[int, dict[int, int]]] = []
    for name, lines in covered.items():
        if path.endswith("/" + name) or name.endswith("/" + path):
            best.append((min(len(name), len(path)), lines))
    if not best:
        return None
    best.sort(key=lambda pair: pair[0], reverse=True)
    if len(best) > 1 and best[0][0] == best[1][0]:
        return None  # two equally good candidates: report it as not measured
    return best[0][1]


def report_digest(cfg: Config) -> str | None:
    """Hash of the coverage report as the passing gate left it, or None when unused."""
    if cfg.coverage_mode == "off" or not cfg.coverage_report:
        return None
    report = cfg.root / cfg.coverage_report
    try:
        ensure_safe(cfg.root, report)
        return hashlib.sha256(report.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


Source = tuple[str, str | None, str]


def _sources(cfg: Config, state: dict[str, Any], given: list[Source] | None) -> list[Source]:
    return list(scope.changed_sources(cfg, state)) if given is None else given


def uncovered(
    cfg: Config, state: dict[str, Any], final: bool = False, sources: list[Source] | None = None
) -> list[str]:
    """Changed lines no test ran, from the project's coverage report; empty when not set up.

    With ``final`` (at `sdd done`) the report must also be the one the last passing gate
    left behind, so a report edited by hand afterwards does not count.
    """
    if cfg.coverage_mode == "off" or not cfg.coverage_report:
        return []
    seen = (state.get("gate") or {}).get("coverage")
    if final and seen != report_digest(cfg):
        return ["the coverage report is not the one the last passing gate wrote; run the gate"]
    report = cfg.root / cfg.coverage_report
    try:
        ensure_safe(cfg.root, report)
    except ValueError:
        return [f"the coverage report {cfg.coverage_report} is outside the project"]
    if not report.is_file():
        return [f"no coverage report at {cfg.coverage_report}; run the gate"]
    sources = _sources(cfg, state, sources)
    if not sources:
        return []
    if report.stat().st_mtime < max((cfg.root / p).stat().st_mtime for p, _b, _a in sources):
        return ["the coverage report is older than the changes; run the gate again"]
    try:
        covered = read_coverage(report)
    except (OSError, ValueError, ET.ParseError):
        return [f"the coverage report {cfg.coverage_report} cannot be read"]
    found: list[str] = []
    for path, before, after in sources:
        lines = _lookup(covered, path)
        if lines is None:
            if path.endswith(CODE_SUFFIXES):
                found.append(f"{path} is not in the coverage report (no test loads it)")
            continue
        bad = sorted(n for n in added_lines(before, after) if lines.get(n, 1) == 0)
        if bad:
            found.append(f"{path}: lines {_ranges(bad)} changed but no test runs them")
    return found


# --- side-effect surface -------------------------------------------------------------------


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _opens_for_writing(call: ast.Call) -> bool:
    values = [*call.args[1:2], *(k.value for k in call.keywords if k.arg == "mode")]
    return any(
        isinstance(v, ast.Constant) and isinstance(v.value, str) and set(v.value) & set("wax+")
        for v in values
    )


def _py_surface(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return set()
    found: set[str] = set()
    typing_only = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _dotted(node.test).endswith("TYPE_CHECKING")
        for child in node.body
        for inner in ast.walk(child)
    }
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue  # imported for annotations only: never runs
        if isinstance(node, ast.Import):
            found.update(
                f"import {a.name.split('.')[0]}" for a in node.names if a.name not in PY_SAFE
            )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = {f"{node.module}.{alias.name}" for alias in node.names}
            if node.module not in PY_SAFE and not names <= PY_SAFE:
                found.add(f"import {node.module.split('.')[0]}")
            for name in names & PY_CALLS:  # `from os import system` hides the full name
                found.add(f"call {name}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in PY_CALLS:
                found.add(f"call {name}")
            elif name == "open" and _opens_for_writing(node):
                found.add("call open (for writing)")
    return {item for item in found if not item.startswith("import ") or item[7:] in PY_MODULES}


def _js_surface(source: str) -> set[str]:
    found = {f"import {m}" for m in (*JS_REQUIRE.findall(source), *JS_IMPORT.findall(source))}
    found.update(f"call {a or b}" for a, b in JS_CALLS.findall(source))
    return found


def surface(path: str, source: str | None) -> set[str]:
    if source is None:
        return set()
    if path.endswith(".py"):
        return _py_surface(source)
    return _js_surface(source) if path.endswith(CODE_SUFFIXES) else set()


def _named(item: str, named: set[str]) -> bool:
    token = item.split(" ", 1)[1].split(" ")[0]
    return bool({token, token.split(".")[-1]} & named)


def new_surface(
    cfg: Config, state: dict[str, Any], sources: list[Source] | None = None
) -> list[str]:
    """New imports of, or calls to, processes, the network, files, `eval`: unless named."""
    if cfg.surface_mode == "off":
        return []
    spec_dir = cfg.root / str(state["spec_dir"])
    texts = [
        (spec_dir / name).read_text(encoding="utf-8") if (spec_dir / name).exists() else ""
        for name in ("plan.md", "tasks.md")
    ]
    named = scope.named_symbols(texts[0], texts[1])
    found: list[str] = []
    for path, before, after in _sources(cfg, state, sources):
        for item in sorted(surface(path, after) - surface(path, before)):
            if not _named(item, named):
                found.append(f"{path}: new {item}")
    return found


# --- growth budget -------------------------------------------------------------------------


def _sizes(source: str | None) -> dict[str, int]:
    """Syntax-tree nodes per function and method, by qualified name."""
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    sizes: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, funcs):
            sizes[node.name] = sum(1 for _ in ast.walk(node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, funcs):
                    sizes[f"{node.name}.{item.name}"] = sum(1 for _ in ast.walk(item))
    return sizes


def grown(cfg: Config, state: dict[str, Any], sources: list[Source] | None = None) -> list[str]:
    """Functions that grew by more than the budget: a named function can still sprawl."""
    if cfg.growth_mode == "off" or cfg.max_growth < 1:
        return []
    found: list[str] = []
    for path, before, after in _sources(cfg, state, sources):
        if before is None or not path.endswith(".py"):
            continue
        old = _sizes(before)
        for name, size in _sizes(after).items():
            if name in old and size - old[name] > cfg.max_growth:
                found.append(f"{path}::{name} grew by {size - old[name]} nodes")
    return found


# --- findings ------------------------------------------------------------------------------


NO_BASE = (
    "nothing to compare against: the run started in a repository with no commit, so changed "
    "lines, new side effects and growth cannot be checked. Commit, then start the run again"
)


def _active(cfg: Config) -> bool:
    coverage = cfg.coverage_mode != "off" and bool(cfg.coverage_report)
    return coverage or cfg.surface_mode != "off" or cfg.growth_mode != "off"


def _has_base(state: dict[str, Any]) -> bool:
    return bool((state.get("scope") or {}).get("base"))


def _checks(cfg: Config, final: bool) -> list[tuple[str, str, Any]]:
    return [
        (cfg.coverage_mode, "uncovered change", lambda c, s, src: uncovered(c, s, final, src)),
        (cfg.surface_mode, "new side effect", new_surface),
        (cfg.growth_mode, "large change", grown),
    ]


def notes(cfg: Config, state: dict[str, Any], final: bool = False) -> list[str]:
    """One line per finding, in warn and enforce mode, for `sdd check` and `sdd scope`."""
    if not _active(cfg):
        return []
    if not _has_base(state):
        return [f"not checked: {NO_BASE}"]
    sources = list(scope.changed_sources(cfg, state))  # read once for all three checks
    lines = [
        f"{label}: {line}"
        for mode, label, check in _checks(cfg, final)
        if mode != "off"
        for line in check(cfg, state, sources)
    ]
    lines += [f"not checked: {path} is too large to read" for path in scope.too_large(cfg, state)]
    return lines


def problem(cfg: Config, state: dict[str, Any]) -> str | None:
    """Why `sdd done` must refuse (enforce mode only), or None."""
    modes = (cfg.surface_mode, cfg.growth_mode)
    enforced = "enforce" in modes or (cfg.coverage_mode == "enforce" and cfg.coverage_report)
    if not enforced:
        return None
    if not _has_base(state):
        return NO_BASE
    sources = list(scope.changed_sources(cfg, state))
    for mode, label, check in _checks(cfg, True):
        if mode != "enforce":
            continue
        found = check(cfg, state, sources)
        if found:
            shown = "; ".join(found[:4]) + (f" and {len(found) - 4} more" if len(found) > 4 else "")
            return (
                f"{label}: {shown}. Fix it (a test that runs the lines, naming the item in "
                f"{state['spec_dir']}/plan.md and approving again, or a smaller change)"
            )
    return None
