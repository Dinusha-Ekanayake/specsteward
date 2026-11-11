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
from collections import Counter
from pathlib import Path
from typing import Any

from specsteward import scope
from specsteward.config import Config
from specsteward.safefs import ensure_safe

MAX_REPORT_BYTES = 20_000_000
MAX_DIFF_LINES = 20_000  # beyond this every line counts as changed; difflib is quadratic
CODE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")

PY_MODULES = frozenset(
    ("subprocess", "socket", "urllib", "http", "requests", "httpx", "aiohttp", "ctypes")
    + ("pickle", "marshal", "shelve", "ftplib", "smtplib", "telnetlib", "paramiko")
    + ("multiprocessing", "webbrowser", "importlib")
)
PY_CALLS = frozenset(
    ("os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.removedirs")
    + ("os.kill", "os.startfile", "os.open", "os.rename", "os.replace", "os.chmod")
    + ("shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile")
    + ("shutil.copytree", "eval", "exec", "compile", "__import__")
    + ("builtins.eval", "builtins.exec", "builtins.compile", "builtins.__import__")
)
PY_CALL_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
PY_WRITE_METHODS = frozenset(("write_text", "write_bytes", "unlink", "rmtree", "rmdir"))
# Parts of a watched package that only handle text or constants.
PY_SAFE = frozenset(("urllib.parse", "urllib.error", "http.cookies", "http.HTTPStatus"))
PY_SAFE |= frozenset(("http.HTTPMethod", "importlib.metadata", "importlib.resources"))
JS_MODULES = "child_process|fs/promises|fs|net|http|https|dgram|vm|worker_threads|cluster"
JS_REQUIRE = re.compile(rf"""require\(\s*['"](?:node:)?({JS_MODULES})['"]\s*\)""")
JS_IMPORT = re.compile(rf"""from\s+['"](?:node:)?({JS_MODULES})['"]""")
JS_DYNAMIC = re.compile(rf"""import\(\s*['"](?:node:)?({JS_MODULES})['"]\s*\)""")
JS_CALLS = re.compile(
    r"\b(eval|fetch|Bun\.spawn|Deno\.run|Deno\.Command|new Function|new WebSocket"
    r"|new XMLHttpRequest)\s*\("
)


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
    try:
        newest = max(_mtime(cfg.root / p) for p, _b, _a in sources)
        if report.stat().st_mtime < newest:
            return ["the coverage report is older than the changes; run the gate again"]
        covered = read_coverage(report)
    except (OSError, ValueError, OverflowError, ET.ParseError):
        return [f"the coverage report {cfg.coverage_report} cannot be read"]
    found: list[str] = []
    for path, before, after in sources:
        lines = _lookup(covered, path)
        if lines is None:
            if path.endswith(CODE_SUFFIXES):
                found.append(f"{path} is not in the coverage report (no test loads it)")
            continue
        changed = added_lines(before, after)
        bad = sorted(n for n in changed if lines.get(n, 1) == 0)
        if bad:
            found.append(f"{path}: lines {_ranges(bad)} changed but no test runs them")
        elif _code_lines(after, changed) >= MIN_UNMEASURED and not changed & lines.keys():
            # the file is listed, yet not one changed line is: not the report of this code
            found.append(f"{path}: the report measures none of the changed lines")
    return found


MIN_UNMEASURED = 5


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0  # deleted since it was listed


def _code_lines(text: str, numbers: set[int]) -> int:
    """How many of these lines hold code rather than blanks or comments."""
    lines = _lines(text)
    comment = ("#", "//", "/*", "*", '"""', "'''")
    return sum(
        1
        for n in numbers
        if 0 < n <= len(lines)
        and lines[n - 1].strip()
        and not lines[n - 1].lstrip().startswith(comment)
    )


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
    """A write mode, or a mode that is not a literal and so cannot be read here."""
    values = [*call.args[1:2], *(k.value for k in call.keywords if k.arg == "mode")]
    return any(
        not isinstance(v, ast.Constant)
        or (isinstance(v.value, str) and bool(set(v.value) & set("wax+")))
        for v in values
    )


def _aliases(tree: ast.AST, skip: set[int]) -> dict[str, str]:
    """Local name -> what it really is (`import os as o`, `from os import system as run`)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _resolve(name: str, aliases: dict[str, str]) -> str:
    head, _, rest = name.partition(".")
    real = aliases.get(head, head)
    return f"{real}.{rest}" if rest else real


def _safe(name: str) -> bool:
    return any(name == safe or name.startswith(safe + ".") for safe in PY_SAFE)


def _call_item(node: ast.Call, aliases: dict[str, str]) -> str | None:
    name = _resolve(_dotted(node.func), aliases)
    if name in PY_CALLS or name.startswith(PY_CALL_PREFIXES):
        return f"call {name}"
    if name in ("open", "io.open", "builtins.open"):
        return "call open (for writing)" if _opens_for_writing(node) else None
    if name == "getattr" and node.args:
        target = _resolve(_dotted(node.args[0]), aliases).split(".")[0]
        if target in PY_MODULES or target in ("os", "shutil", "builtins", "sys"):
            return f"call getattr (on {target})"
    if name.split(".")[0] in PY_MODULES and not _safe(name):
        return f"call {name}"
    if isinstance(node.func, ast.Attribute) and node.func.attr in PY_WRITE_METHODS:
        return f"call .{node.func.attr}"
    return None


def _py_surface(source: str) -> Counter[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Counter()
    found: Counter[str] = Counter()
    typing_only = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _dotted(node.test).endswith("TYPE_CHECKING")
        for child in node.body
        for inner in ast.walk(child)
    }
    aliases = _aliases(tree, typing_only)
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue  # imported for annotations only: never runs
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in PY_MODULES and not _safe(alias.name):
                    found[f"import {root}"] += 1
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = {f"{node.module}.{alias.name}" for alias in node.names}
            root = node.module.split(".")[0]
            if root in PY_MODULES and not _safe(node.module) and not all(map(_safe, names)):
                found[f"import {root}"] += 1
            for name in names:  # `from os import system` hides the call's full name
                if name in PY_CALLS or name.startswith(PY_CALL_PREFIXES):
                    found[f"call {name}"] += 1
        elif isinstance(node, ast.Call):
            item = _call_item(node, aliases)
            if item:
                found[item] += 1
    return found


def _js_surface(source: str) -> Counter[str]:
    found: Counter[str] = Counter()
    for pattern in (JS_REQUIRE, JS_IMPORT, JS_DYNAMIC):
        found.update(f"import {name}" for name in pattern.findall(source))
    found.update(f"call {name}" for name in JS_CALLS.findall(source))
    return found


def surface(path: str, source: str | None) -> Counter[str]:
    """What the file reaches out with, and how often: imports of, and calls to, processes,
    the network, the filesystem and code evaluation. A list of known names, not a proof."""
    if source is None:
        return Counter()
    if path.endswith(".py"):
        return _py_surface(source)
    return _js_surface(source) if path.endswith(CODE_SUFFIXES) else Counter()


def _named(item: str, named: set[str]) -> bool:
    """The plan or the tasks name this item: the call (`os.system`, `system`), or for a
    watched package its own name (`subprocess` covers `subprocess.run`). Naming `os` does
    not cover `os.system`."""
    token = item.split(" ", 1)[1].split(" ")[0].lstrip(".")
    root = token.split(".")[0]
    return bool({token, token.split(".")[-1]} & named) or (root in PY_MODULES and root in named)


def _plan_names(cfg: Config, state: dict[str, Any]) -> set[str]:
    spec_dir = cfg.root / str(state["spec_dir"])
    texts = [
        (spec_dir / name).read_text(encoding="utf-8") if (spec_dir / name).exists() else ""
        for name in ("plan.md", "tasks.md")
    ]
    return scope.named_symbols(texts[0], texts[1])


def new_surface(
    cfg: Config, state: dict[str, Any], sources: list[Source] | None = None
) -> list[str]:
    """New imports of, or calls to, processes, the network, files, `eval`: unless named.

    Counted, so one more `os.system` in a file that already had one is still new. Files
    named like tests but kept outside a test folder are checked too: they are code a planned
    file could import.
    """
    if cfg.surface_mode == "off":
        return []
    named = _plan_names(cfg, state)
    found: list[str] = []
    for path, before, after in (*_sources(cfg, state, sources), *scope.stray_tests(cfg, state)):
        was = surface(path, before)
        for item, count in sorted(surface(path, after).items()):
            extra = count - was.get(item, 0)
            if extra > 0 and not _named(item, named):
                found.append(f"{path}: new {item}" + (f" (x{extra})" if extra > 1 else ""))
    return found


# --- growth budget -------------------------------------------------------------------------

NEW_FUNCTION_FACTOR = 3  # a new, unnamed function may be this many budgets before it is listed


def _sizes(source: str | None) -> dict[str, int]:
    """Syntax-tree nodes per function and method (nested classes included), by qualified
    name. A function defined inside another one counts toward its parent."""
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    sizes: dict[str, int] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, funcs):
                sizes[prefix + node.name] = sum(1 for _ in ast.walk(node))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return sizes


def grown(cfg: Config, state: dict[str, Any], sources: list[Source] | None = None) -> list[str]:
    """Functions that grew by more than the budget, and large new functions the plan does
    not name (a private helper is where unplanned behaviour would otherwise hide)."""
    if cfg.growth_mode == "off" or cfg.max_growth < 1:
        return []
    named = _plan_names(cfg, state)
    found: list[str] = []
    for path, before, after in _sources(cfg, state, sources):
        if before is None or not path.endswith(".py"):
            continue  # a new file: the plan names the file itself
        old = _sizes(before)
        for name, size in _sizes(after).items():
            if name in old:
                if size - old[name] > cfg.max_growth:
                    found.append(f"{path}::{name} grew by {size - old[name]} nodes")
            elif size > cfg.max_growth * NEW_FUNCTION_FACTOR and not (
                {name, name.split(".")[-1]} & named
            ):
                found.append(f"{path}::{name} is new, unnamed in the plan, and {size} nodes")
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
