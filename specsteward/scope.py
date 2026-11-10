"""Scope guard: a run may only change the files its approved plan names.

The approval hash proves the spec, plan and task table were not edited. It cannot tell that
the *code* wandered outside what they allow, so this checks the working tree against the
files the plan names (`Modules / files`), plus a small allow-list (tests, the spec folder).
To widen the scope, the plan has to change, which breaks the approval digest, so the user
approves again before the run can go on.
"""

import ast
import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from specsteward.config import Config
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


def snapshot(root: Path) -> dict[str, Any]:
    """Taken when a run starts: where it began, and what was already dirty."""
    base = head(root)
    return {"base": base, "dirty": sorted(_changed_now(root, base))}


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
    skip = set(start.get("dirty", []))
    changed = _changed_now(cfg.root, start.get("base"))
    return sorted(
        path
        for path in changed - skip
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


def named_symbols(*texts: str) -> set[str]:
    """Identifiers the plan and tasks mention in backticks (`login`, `Auth.login()`, `a.py::f`)."""
    names: set[str] = set()
    for text in texts:
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
    named = named_symbols(plan_text, tasks.read_text(encoding="utf-8") if tasks.exists() else "")
    planned = planned_paths(plan_text)
    out: list[str] = []
    for path in sorted(_changed_now(cfg.root, base) - set(start.get("dirty", []))):
        file = cfg.root / path
        if _ignored(path) or not file.is_file() or any(_matches(path, a) for a in DEFAULT_ALLOW):
            continue
        if not any(_matches(path, pattern) for pattern in planned):
            continue  # outside the plan: the file check reports it
        before = _text_at(cfg.root, base, path)
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
