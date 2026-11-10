"""A fingerprint of the working tree, so a gate result can be tied to the code it saw.

``sdd done`` only ticks a task when the last passing check and the last clean review were
both recorded for the code as it is now. The fingerprint covers tracked changes and
untracked files; run state and the task table being ticked are left out.
"""

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

# Never part of the fingerprint, whatever the project's .gitignore says: specsteward's own
# state, the code graph, environments, and files that tools regenerate when tests run.
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
SKIP_DIRS |= {".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox"}
# Environments and our own state are skipped at the top level only: a folder called `venv`
# deeper down (`src/venv/`) is project code and must not be invisible to the gate and scope.
TOP_LEVEL_ENV = {".sdd", ".venv", "venv", "graphify-out"}
# Build output is only skipped at the top level (as the git pathspec below does): a source
# package that happens to be called `out` or `coverage` deeper down is part of the fingerprint.
TOP_LEVEL_SKIP = {"htmlcov", "coverage", ".nyc_output", "dist", "build", "target", ".next", "out"}
SKIP_FILES = {".coverage", "coverage.xml", ".graphifyignore", ".DS_Store"}


def _skipped(rel: str, excluded: set[str]) -> bool:
    parts = rel.split("/")
    return (
        rel in excluded
        or parts[-1] in SKIP_FILES
        or bool(SKIP_DIRS & set(parts[:-1]))
        or (len(parts) > 1 and parts[0] in TOP_LEVEL_SKIP)
        or parts[0] in TOP_LEVEL_ENV
    )


def _git(root: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _hash_files(root: Path, paths: list[Path], update: Callable[[bytes], None]) -> None:
    for path in sorted(paths):
        update(path.relative_to(root).as_posix().encode() + b"\0")
        try:
            update(path.read_bytes())
        except OSError:
            update(b"<unreadable>")
        update(b"\0")


def fingerprint(root: Path, exclude: tuple[str, ...] = ()) -> str:
    """sha256 of the uncommitted state of ``root``; ``exclude`` lists relative paths."""
    digest = hashlib.sha256()
    excluded = set(exclude)
    skip = [
        f":(exclude){name}"
        for name in sorted(SKIP_DIRS | TOP_LEVEL_SKIP | TOP_LEVEL_ENV | SKIP_FILES)
    ]
    pathspec = [".", *(f":(exclude){rel}" for rel in exclude), *skip]
    diff = _git(root, "diff", "HEAD", "--binary", "--", *pathspec)
    listing = ["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec]
    if diff is None:
        # No commit yet (or no git): hash the files git would track, else every file.
        listing.insert(1, "--cached")
    names = _git(root, *listing)
    if names is not None:
        digest.update(b"git\0" + (diff or b""))
        rels = [name for name in names.decode("utf-8", "replace").split("\0") if name]
        _hash_files(
            root, [root / rel for rel in rels if not _skipped(rel, excluded)], digest.update
        )
        return digest.hexdigest()
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not _skipped(path.relative_to(root).as_posix(), excluded)
    ]
    digest.update(b"files\0")
    _hash_files(root, files, digest.update)
    return digest.hexdigest()
