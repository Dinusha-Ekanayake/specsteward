"""Scope guard: a run may only change the files its approved plan names.

The approval hash proves the spec, plan and task table were not edited. It cannot tell that
the *code* wandered outside what they allow, so this checks the working tree against the
files the plan names (`Modules / files`), plus a small allow-list (tests, the spec folder).
To widen the scope, the plan has to change, which breaks the approval digest, so the user
approves again before the run can go on.
"""

import ast
import hashlib
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from specsteward import gitsafe
from specsteward.config import Config
from specsteward.safefs import ensure_safe
from specsteward.worktree import _skipped

DEFAULT_ALLOW = (
    "tests/**",
    "test/**",
    "**/__tests__/**",
    "**/test_*",
    "**/*_test.*",
    "**/*.test.*",
    "**/*.spec.*",
    "CHANGELOG*",
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_PATHLIKE = re.compile(r"^[\w.\-/*?\[\]{}]+$")


def _git(root: Path, *args: str) -> list[str] | None:
    raw = gitsafe.output(root, *args)
    if raw is None:
        return None
    return [line for line in raw.decode("utf-8", "replace").split("\0") if line]


def head(root: Path) -> str | None:
    found = _git(root, "rev-parse", "HEAD")
    return found[0].strip() if found else None


def _changed_now(root: Path, base: str | None) -> set[str]:
    tracked = (
        _git(root, "diff", "--name-only", "--relative", "--no-renames", "-z", base or "HEAD")
        if base
        else []
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None:
        return set()
    hidden = gitsafe.hidden_edits(root)  # edits git was told not to report
    return {path.replace("\\", "/") for path in (*tracked, *untracked, *hidden)}


BASE_DIR = Path(".sdd") / "scope-base"
MAX_COPIES = 200
MAX_COPY_BYTES = 1_000_000


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""  # deleted, or unreadable


def snapshot(root: Path) -> dict[str, Any]:
    """Taken when a run starts: where it began, and what was already dirty.

    A file that was already changed is remembered by content, with a copy of small ones, so
    the run is blamed only for what it does to it, not for the edits that came first.
    """
    base = head(root)
    dirty: dict[str, str] = {}
    for count, rel in enumerate(sorted(_changed_now(root, base))):
        source = root / rel
        dirty[rel] = _digest(source)
        try:
            if count < MAX_COPIES and source.is_file() and source.stat().st_size <= MAX_COPY_BYTES:
                copy = root / BASE_DIR / rel
                ensure_safe(root, copy)
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(source.read_bytes())
        except (OSError, ValueError):
            continue  # no copy: the symbol check skips this file
    return {"base": base, "dirty": dirty, "excludes": gitsafe.exclude_state(root)}


def _touched(root: Path, start: dict[str, Any]) -> list[str]:
    """Files the run changed: different from the start, not merely different from HEAD."""
    dirty = start.get("dirty", {})
    if isinstance(dirty, list):  # state written by an older release: whole files are skipped
        dirty = dict.fromkeys(dirty, None)
    found = []
    for path in sorted(_changed_now(root, start.get("base"))):
        if path in dirty and (dirty[path] is None or dirty[path] == _digest(root / path)):
            continue
        found.append(path)
    return found


def planned_paths(plan: str) -> list[str]:
    """Paths and globs the plan names in backticks (the `Modules / files` table, mostly)."""
    found: list[str] = []
    for token in _BACKTICKED.findall(plan):
        token = token.strip().removeprefix("./")
        has_shape = "/" in token or re.search(r"\.\w{1,6}$", token) or "*" in token
        if has_shape and _PATHLIKE.match(token) and token not in found:
            found.append(token)
    return found


def _glob(pattern: str) -> re.Pattern[str]:
    """`**` crosses folders, `*` and `?` stay inside one, so `**/test_*` is a file name."""
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out)


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(char in pattern for char in "*?"):
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern or path.startswith(pattern + "/")


def _ignored(path: str) -> bool:
    return _skipped(path, set())


def out_of_scope(cfg: Config, state: dict[str, Any]) -> list[str]:
    """Files changed since the run began that neither the plan nor the allow-list covers."""
    if cfg.scope_mode == "off":
        return []
    spec_dir = str(state["spec_dir"])
    plan_file = cfg.root / spec_dir / "plan.md"
    if not plan_file.exists():
        return []
    allowed = planned_paths(plan_file.read_text(encoding="utf-8"))
    if not allowed:  # nothing to compare against; a plan with no files is reported elsewhere
        return []
    allowed += [*DEFAULT_ALLOW, *cfg.scope_allow, f"{spec_dir}/"]
    start = state.get("scope") or {}
    return sorted(
        path
        for path in _touched(cfg.root, start)
        if not _ignored(path) and not any(_matches(path, pattern) for pattern in allowed)
    )


def hiding_problem(cfg: Config, state: dict[str, Any]) -> str | None:
    """Git's own "ignore this" settings changed during the run: new files could be invisible
    to the gate, the review and the scope checks. Checked in every mode."""
    start = state.get("scope") or {}
    if start.get("excludes") is None or start["excludes"] == gitsafe.exclude_state(cfg.root):
        return None
    return (
        "git's exclude settings (.git/info/exclude or core.excludesFile) changed during this "
        "run, so new files may be hidden from the checks. Undo the change, or stop the run "
        "and start it again"
    )


def problem(cfg: Config, state: dict[str, Any]) -> str | None:
    """Why the tree is out of scope (enforce mode only), or None."""
    hidden = hiding_problem(cfg, state)
    if hidden:
        return hidden
    if cfg.scope_mode != "enforce":
        return symbol_problem(cfg, state)
    extra = out_of_scope(cfg, state)
    if not extra:
        return symbol_problem(cfg, state)
    shown = ", ".join(extra[:6]) + (f" and {len(extra) - 6} more" if len(extra) > 6 else "")
    return (
        f"changes outside the approved plan: {shown}. Revert them, or add the files to "
        f"{state['spec_dir']}/plan.md and ask the user to approve the spec again"
    )


# --- symbols: growth inside a file the plan already names ---------------------------------

_IDENT = re.compile(r"[A-Za-z_]\w*")
_JS_DECL = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)


def _files_section(plan: str) -> str:
    """The plan's `Modules / files` section; the whole plan when it has no such heading."""
    lines: list[str] = []
    inside = found = False
    for line in plan.splitlines():
        if line.startswith("#"):
            inside = any(word in line.lower() for word in ("modules", "files"))
            found = found or inside
        elif inside:
            lines.append(line)
    return "\n".join(lines) if found else plan


def named_symbols(plan: str, tasks: str) -> set[str]:
    """Identifiers that the plan's file table, a `path::name` mention or a task row backticks.

    A passing mention elsewhere (a risk note, the approach) does not count as naming it.
    """
    rows = [line for line in tasks.splitlines() if line.lstrip().startswith("|")]
    pairs = [line for line in plan.splitlines() if "::" in line]
    names: set[str] = set()
    for text in (_files_section(plan), *rows, *pairs):
        for token in _BACKTICKED.findall(text):
            names.update(_IDENT.findall(token))
    return names


def _py_symbols(source: str) -> dict[str, str]:
    """Public top-level functions, classes and methods, each with a hash of its code."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    found: dict[str, str] = {}
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if not isinstance(node, (*funcs, ast.ClassDef)) or node.name.startswith("_"):
            continue
        if isinstance(node, ast.ClassDef):
            methods = [item for item in node.body if isinstance(item, funcs)]
            node.body = [item for item in node.body if not isinstance(item, funcs)] or [ast.Pass()]
            for method in methods:
                if not method.name.startswith("_"):
                    found[f"{node.name}.{method.name}"] = ast.dump(method)
        found[node.name] = ast.dump(node)
    return found


def _js_symbols(source: str) -> dict[str, str]:
    """Exported names only; a changed body is not seen, a new export is."""
    return {name: "" for name in _JS_DECL.findall(source) if not name.startswith("_")}


def _symbols(path: str, source: str) -> dict[str, str]:
    if path.endswith(".py"):
        return _py_symbols(source)
    if path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs")):
        return _js_symbols(source)
    return {}


def _start_text(root: Path, start: dict[str, Any], path: str) -> str | None:
    """The file as the run found it: the saved copy if it was already dirty, else HEAD."""
    if path in start.get("dirty", {}):
        copy = root / BASE_DIR / path
        try:
            if not copy.is_file():
                return None
            return copy.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return None
    return _text_at(root, str(start["base"]), path)


def _text_at(root: Path, base: str, path: str) -> str | None:
    try:
        proc = subprocess.run(
            [*gitsafe.GIT, "show", f"{base}:./{path}"],  # `./`: relative to the project
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=gitsafe.git_env(),
        )
    except OSError:
        return None
    return proc.stdout.removeprefix("\ufeff") if proc.returncode == 0 else None


def _renames(root: Path, base: str) -> dict[str, str]:
    """New path -> old path for files git sees as moved since ``base``."""
    found = _git(root, "diff", "--name-status", "--relative", "-M", "-z", base) or []
    moved: dict[str, str] = {}
    index = 0
    while index < len(found):
        if found[index].startswith(("R", "C")) and index + 2 < len(found):
            moved[found[index + 2].replace("\\", "/")] = found[index + 1].replace("\\", "/")
            index += 3
        else:
            index += 2
    return moved


def unplanned_symbols(cfg: Config, state: dict[str, Any]) -> list[str]:
    """Public symbols added or changed in files that existed before the run and that neither
    the plan nor the tasks name. New files are not checked: the plan names the file."""
    if cfg.symbol_mode == "off":
        return []
    spec_dir = cfg.root / str(state["spec_dir"])
    plan = spec_dir / "plan.md"
    start = state.get("scope") or {}
    base = start.get("base")
    if not base or not plan.exists():
        return []
    plan_text = plan.read_text(encoding="utf-8")
    tasks = spec_dir / "tasks.md"
    tasks_text = tasks.read_text(encoding="utf-8") if tasks.exists() else ""
    named = named_symbols(plan_text, tasks_text)
    planned = planned_paths(plan_text)
    out: list[str] = []
    for path in _touched(cfg.root, start):
        file = cfg.root / path
        if _ignored(path) or not file.is_file() or any(_matches(path, a) for a in DEFAULT_ALLOW):
            continue
        if not any(_matches(path, pattern) for pattern in planned):
            continue  # outside the plan: the file check reports it
        before = _start_text(cfg.root, start, path)
        if before is None:
            continue
        old = _symbols(path, before)
        new = _symbols(path, file.read_text(encoding="utf-8", errors="replace"))
        for name, digest in new.items():
            if old.get(name) != digest and not ({name, name.split(".")[-1]} & named):
                out.append(f"{path}::{name}")
    return out


def symbol_problem(cfg: Config, state: dict[str, Any]) -> str | None:
    if cfg.symbol_mode != "enforce":
        return None
    extra = unplanned_symbols(cfg, state)
    if not extra:
        return None
    shown = ", ".join(extra[:6]) + (f" and {len(extra) - 6} more" if len(extra) > 6 else "")
    return (
        f"unplanned changes inside planned files: {shown}. Name them in "
        f"{state['spec_dir']}/plan.md or tasks.md in backticks and ask the user to approve again, "
        "or revert them"
    )


def allowed_unplanned(cfg: Config, state: dict[str, Any]) -> list[str]:
    """Changed files that only the allow-list (tests, changelog, [scope] allow) permits."""
    if cfg.scope_mode == "off":
        return []
    plan_file = cfg.root / str(state["spec_dir"]) / "plan.md"
    planned = planned_paths(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else []
    spec_dir = f"{state['spec_dir']}/"
    allow = (*DEFAULT_ALLOW, *cfg.scope_allow)
    return [
        path
        for path in _touched(cfg.root, state.get("scope") or {})
        if not _ignored(path)
        and not path.startswith(spec_dir)
        and not any(_matches(path, pattern) for pattern in planned)
        and any(_matches(path, pattern) for pattern in allow)
    ]


def changed_sources(cfg: Config, state: dict[str, Any]) -> Iterator[tuple[str, str | None, str]]:
    """Source files the run changed (not tests, the spec folder or tool folders), each with
    its text at the start of the run (None for a new file) and its text now."""
    start = state.get("scope") or {}
    if not start.get("base"):
        return
    moved = _renames(cfg.root, str(start["base"]))
    for path in _source_paths(cfg, state):
        file = cfg.root / path
        try:
            if file.stat().st_size > MAX_COPY_BYTES:
                continue  # generated or binary: `too_large` reports it
            after = file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        before = _start_text(cfg.root, start, path)
        if before is None and path in moved:  # a moved file is not a new one
            before = _start_text(cfg.root, start, moved[path])
        yield path, before, after


def _source_paths(cfg: Config, state: dict[str, Any]) -> list[str]:
    spec_dir = f"{state['spec_dir']}/"
    return [
        path
        for path in _touched(cfg.root, state.get("scope") or {})
        if not _ignored(path)
        and not path.startswith(spec_dir)
        and (cfg.root / path).is_file()
        and not any(_matches(path, pattern) for pattern in DEFAULT_ALLOW)
    ]


TEST_FOLDERS = frozenset(("tests", "test", "__tests__", "spec", "specs", "e2e", "testing"))


def stray_tests(cfg: Config, state: dict[str, Any]) -> Iterator[tuple[str, str | None, str]]:
    """Changed files named like tests (`helper_test.py`) that are not in a test folder. The
    allow-list lets them through unplanned, so the side-effect check still reads them."""
    start = state.get("scope") or {}
    if not start.get("base"):
        return
    spec_dir = f"{state['spec_dir']}/"
    for path in _touched(cfg.root, start):
        file = cfg.root / path
        if _ignored(path) or path.startswith(spec_dir) or not file.is_file():
            continue
        if TEST_FOLDERS & set(path.split("/")[:-1]):
            continue
        if not any(_matches(path, pattern) for pattern in DEFAULT_ALLOW):
            continue
        try:
            if file.stat().st_size > MAX_COPY_BYTES:
                continue
            after = file.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        yield path, _start_text(cfg.root, start, path), after


def too_large(cfg: Config, state: dict[str, Any]) -> list[str]:
    """Changed source files too big to be read line by line, so nothing checks inside them."""
    found = []
    for path in _source_paths(cfg, state):
        try:
            if (cfg.root / path).stat().st_size > MAX_COPY_BYTES:
                found.append(path)
        except OSError:
            continue
    return found


def notes(cfg: Config, state: dict[str, Any]) -> list[str]:
    """One line per finding, for `sdd check` and `sdd scope`; empty when all is inside."""
    hidden = hiding_problem(cfg, state)
    lines = [hidden] if hidden else []
    lines += [f"outside the plan: {path}" for path in out_of_scope(cfg, state)]
    lines += [f"unplanned symbol: {name}" for name in unplanned_symbols(cfg, state)]
    return lines
