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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from specsteward.config import Config
from specsteward.safefs import ensure_safe
from specsteward.worktree import SKIP_DIRS, SKIP_FILES, TOP_LEVEL_SKIP

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
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False, text=True, encoding="utf-8"
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.split("\0") if line]


def head(root: Path) -> str | None:
    found = _git(root, "rev-parse", "HEAD")
    return found[0].strip() if found else None


def _changed_now(root: Path, base: str | None) -> set[str]:
    tracked = _git(root, "diff", "--name-only", "-z", base or "HEAD") if base else []
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None:
        return set()
    return {path.replace("\\", "/") for path in (*tracked, *untracked)}


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
    return {"base": base, "dirty": dirty}


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


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(char in pattern for char in "*?["):
        return fnmatchcase(path, pattern) or fnmatchcase(path, pattern.replace("**/", ""))
    return path == pattern or path.startswith(pattern + "/")


def _ignored(path: str) -> bool:
    parts = path.split("/")
    return (
        parts[-1] in SKIP_FILES
        or bool(SKIP_DIRS & set(parts[:-1]))
        or (len(parts) > 1 and parts[0] in TOP_LEVEL_SKIP)
    )


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


def problem(cfg: Config, state: dict[str, Any]) -> str | None:
    """Why the tree is out of scope (enforce mode only), or None."""
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
    except (SyntaxError, ValueError):
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
            return copy.read_text(encoding="utf-8", errors="replace") if copy.is_file() else None
        except OSError:
            return None
    return _text_at(root, str(start["base"]), path)


def _text_at(root: Path, base: str, path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "show", f"{base}:{path}"],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


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


def notes(cfg: Config, state: dict[str, Any]) -> list[str]:
    """One line per finding, for `sdd check` and `sdd scope`; empty when all is inside."""
    lines = [f"outside the plan: {path}" for path in out_of_scope(cfg, state)]
    lines += [f"unplanned symbol: {name}" for name in unplanned_symbols(cfg, state)]
    return lines
